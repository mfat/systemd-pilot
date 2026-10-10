"""Parsers for systemctl and journalctl output.

Everything here is pure: text in, data out. This keeps it easy to test
and independent of how the command was run (locally or over SSH).
"""

from __future__ import annotations

import json
import re
from datetime import datetime

from .errors import ParseError
from .models import LogEntry, Unit

_ANSI_RE = re.compile(r"(?:\x1B[@-Z\\-_]|[\x80-\x9A\x9C-\x9F]|(?:\x1B\[|\x9B)[0-?]*[ -/]*[@-~])")


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _parse_json_list(text: str) -> list[dict] | None:
    text = strip_ansi(text).strip()
    if not text:
        return []
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, list):
        raise ParseError("Expected a JSON list from systemctl")
    return [d for d in data if isinstance(d, dict)]


def parse_list_units(text: str) -> list[Unit]:
    """Parse ``systemctl list-units`` output (JSON, or plain as a fallback)."""
    rows = _parse_json_list(text)
    if rows is not None:
        return [
            Unit(
                name=r.get("unit", ""),
                description=r.get("description", ""),
                load_state=r.get("load", ""),
                active_state=r.get("active", ""),
                sub_state=r.get("sub", ""),
            )
            for r in rows
            if r.get("unit")
        ]

    # systemd < 246 has no JSON output: "UNIT LOAD ACTIVE SUB DESCRIPTION…"
    units = []
    for line in strip_ansi(text).splitlines():
        parts = line.lstrip("●* ").split(None, 4)
        if len(parts) < 4:
            continue
        name, load, active, sub = parts[:4]
        units.append(Unit(name, parts[4] if len(parts) > 4 else "", load, active, sub))
    return units


def parse_list_unit_files(text: str) -> dict[str, str]:
    """Parse ``systemctl list-unit-files`` output into ``{name: state}``."""
    rows = _parse_json_list(text)
    if rows is not None:
        return {r["unit_file"]: r.get("state", "") for r in rows if r.get("unit_file")}

    states = {}
    for line in strip_ansi(text).splitlines():
        parts = line.split()
        if len(parts) >= 2:
            states[parts[0]] = parts[1]
    return states


def merge_units(loaded: list[Unit], files: dict[str, str], include_unloaded: bool) -> list[Unit]:
    """Combine loaded units with unit-file states, one entry per unit.

    Units that have a unit file but are not loaded are added only when
    ``include_unloaded`` is true.
    """
    merged: dict[str, Unit] = {}
    for unit in loaded:
        merged[unit.name] = Unit(
            name=unit.name,
            description=unit.description,
            load_state=unit.load_state,
            active_state=unit.active_state,
            sub_state=unit.sub_state,
            file_state=files.get(unit.name, unit.file_state or ""),
        )
    if include_unloaded:
        for name, state in files.items():
            # Templates ("foo@.service") cannot be started without an instance name.
            if name not in merged and not name.endswith("@.service"):
                merged[name] = Unit(
                    name=name,
                    load_state="not-loaded",
                    active_state="inactive",
                    sub_state="dead",
                    file_state=state,
                )
    return sorted(merged.values(), key=lambda u: u.name.lower())


def parse_properties(text: str) -> dict[str, str]:
    """Parse ``systemctl show`` ``Key=Value`` lines."""
    props = {}
    for line in strip_ansi(text).splitlines():
        key, sep, value = line.partition("=")
        if sep:
            props[key] = value
    return props


def parse_show_units(text: str) -> dict[str, dict[str, str]]:
    """Parse ``systemctl show`` for several units, keyed by their ``Id``.

    systemd separates the units' blocks with an empty line.
    """
    units = {}
    for block in re.split(r"\n\s*\n", strip_ansi(text)):
        props = parse_properties(block)
        if props.get("Id"):
            units[props["Id"]] = props
    return units


def parse_unix_timestamp(value: str) -> datetime | None:
    """Parse a ``systemctl show --timestamp=unix`` value such as ``@1728450657``."""
    if not value.startswith("@"):
        return None
    try:
        seconds = int(value[1:])
    except ValueError:
        return None
    if seconds <= 0:
        return None
    try:
        return datetime.fromtimestamp(seconds)
    except (OverflowError, OSError, ValueError):
        return None


def parse_int(value: str) -> int | None:
    """An unsigned systemd number, or None when unset ("[not set]", or UINT64_MAX)."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return None if number < 0 or number >= 2**64 - 1 else number


def _journal_field(entry: dict, key: str) -> str:
    value = entry.get(key)
    if value is None:
        return ""
    if isinstance(value, list):
        # Non-UTF-8 fields are encoded as byte arrays; repeated fields as lists of strings.
        if value and all(isinstance(v, int) for v in value):
            return bytes(value).decode("utf-8", "replace")
        return str(value[0]) if value else ""
    return str(value)


def parse_journal(text: str) -> list[LogEntry]:
    """Parse ``journalctl -o json`` output (one JSON object per line)."""
    entries = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        ts_raw = _journal_field(entry, "__REALTIME_TIMESTAMP")
        try:
            timestamp = datetime.fromtimestamp(int(ts_raw) / 1_000_000)
        except (ValueError, OverflowError, OSError):
            timestamp = None
        try:
            priority = int(_journal_field(entry, "PRIORITY") or 6)
        except ValueError:
            priority = 6
        entries.append(
            LogEntry(
                timestamp=timestamp,
                priority=priority,
                identifier=_journal_field(entry, "SYSLOG_IDENTIFIER") or _journal_field(entry, "_COMM"),
                pid=_journal_field(entry, "_PID") or _journal_field(entry, "SYSLOG_PID"),
                message=strip_ansi(_journal_field(entry, "MESSAGE")),
                # systemd's own messages name the unit they are about in UNIT/USER_UNIT.
                unit=_journal_field(entry, "UNIT")
                or _journal_field(entry, "USER_UNIT")
                or _journal_field(entry, "_SYSTEMD_USER_UNIT")
                or _journal_field(entry, "_SYSTEMD_UNIT"),
                boot_id=_journal_field(entry, "_BOOT_ID"),
                kernel=_journal_field(entry, "_TRANSPORT") == "kernel",
            )
        )
    return entries
