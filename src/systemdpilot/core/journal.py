"""Find problems in journal entries, and ready-made searches for common ones.

Everything here is pure, so it works the same on local and remote entries.
The UI turns the results into words.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from .models import LogEntry

ERROR, WARNING = "error", "warning"
WARNING_PRIORITY = 4  # syslog priority of a warning; journalctl --priority=4 keeps it and worse


# -- fetching ---------------------------------------------------------------


def oldest_time(entries: list[LogEntry]) -> datetime | None:
    """When the oldest of ``entries`` (newest first) was logged."""
    return next((e.timestamp for e in reversed(entries) if e.timestamp), None)


def until_before(moment: datetime) -> str:
    """A journalctl --until that ends just before ``moment``; journalctl's own end is inclusive."""
    micros = round(moment.timestamp() * 1_000_000) - 1
    return f"@{micros // 1_000_000}.{micros % 1_000_000:06d}"


# -- presets ----------------------------------------------------------------


@dataclass(frozen=True)
class Preset:
    id: str
    group: str  # id of the group it is listed under
    matches: Callable[[LogEntry, str], bool]  # (entry, this boot's id)
    command: str  # roughly equivalent journalctl matches


def _any(pattern: str) -> Callable[[str], bool]:
    regex = re.compile(pattern, re.IGNORECASE)
    return lambda text: regex.search(text) is not None


def _unit_is(entry: LogEntry, *names: str) -> bool:
    return entry.unit in names


_STORAGE = _any(
    r"I/O error|EXT4-fs error|BTRFS (error|warning)|BTRFS: error|XFS .*(error|corrupt)|critical medium error"
)
_OOM = _any(r"Out of memory|oom-killer|Killed process|oom_reaper")
_CRASH = _any(r"segfault at|general protection fault|trap invalid opcode")
_DRM = _any(r"\b(drm|amdgpu|i915|nouveau|nvidia)\b")
_USB = _any(r"\busb \d|New USB device|USB disconnect")
_MAC = _any(r'type=AVC|apparmor="DENIED"')

PRESETS: tuple[Preset, ...] = (
    Preset(
        "coredump",
        "stability",
        lambda e, _b: e.identifier == "systemd-coredump" or (e.kernel and _CRASH(e.message)),
        "_SYSTEMD_UNIT=systemd-coredump.service + MESSAGE_ID=fc2e22bc6cf647edd827b920322c3022",
    ),
    Preset(
        "oom",
        "stability",
        lambda e, _b: e.identifier == "systemd-oomd" or (e.kernel and _OOM(e.message)),
        '_SYSTEMD_UNIT=systemd-oomd.service + _TRANSPORT=kernel --grep "Out of memory|oom-killer|Killed process"',
    ),
    Preset(
        "storage",
        "stability",
        lambda e, _b: e.identifier == "smartd" or (e.kernel and _STORAGE(e.message)),
        '_SYSTEMD_UNIT=smartd.service + _TRANSPORT=kernel --grep "I/O error|EXT4-fs error|BTRFS: error|XFS"',
    ),
    Preset(
        "sudo",
        "security",
        lambda e, _b: e.identifier in ("sudo", "doas", "pkexec", "polkitd"),
        "_COMM=sudo + _COMM=doas + _COMM=pkexec + _SYSTEMD_UNIT=polkit.service",
    ),
    Preset(
        "ssh",
        "security",
        lambda e, _b: e.identifier in ("sshd", "sshd-session") or _unit_is(e, "ssh.service", "sshd.service"),
        "_SYSTEMD_UNIT=sshd.service + _SYSTEMD_UNIT=ssh.service",
    ),
    Preset(
        "mac",
        "security",
        lambda e, _b: e.identifier in ("audit", "kernel") and _MAC(e.message),
        '_TRANSPORT=audit --grep "type=AVC|apparmor=\\"DENIED\\""',
    ),
    Preset(
        "gpu",
        "desktop",
        lambda e, _b: (
            e.priority <= 4
            and (e.identifier in ("gnome-shell", "kwin_wayland", "sway", "Xorg") or (e.kernel and _DRM(e.message)))
        ),
        "_COMM=gnome-shell + _COMM=kwin_wayland + _COMM=sway + _COMM=Xorg -p warning",
    ),
    Preset(
        "audio",
        "desktop",
        lambda e, _b: e.priority <= 4 and e.identifier in ("pipewire", "wireplumber", "pipewire-pulse"),
        "_SYSTEMD_USER_UNIT=pipewire.service + _SYSTEMD_USER_UNIT=wireplumber.service -p warning",
    ),
    Preset(
        "flatpak",
        "desktop",
        lambda e, _b: e.identifier == "flatpak-session-helper" or e.identifier.startswith("xdg-desktop-portal"),
        "_COMM=flatpak-session-helper + _COMM=xdg-desktop-portal",
    ),
    Preset(
        "network",
        "network",
        lambda e, _b: e.priority <= 5 and e.identifier in ("NetworkManager", "systemd-networkd", "systemd-resolved"),
        "_SYSTEMD_UNIT=NetworkManager.service + _SYSTEMD_UNIT=systemd-networkd.service "
        "+ _SYSTEMD_UNIT=systemd-resolved.service -p notice",
    ),
    Preset(
        "usb",
        "network",
        lambda e, _b: e.identifier == "upowerd" or (e.kernel and _USB(e.message)),
        '_TRANSPORT=kernel --grep "usb [0-9]|New USB device" + _SYSTEMD_UNIT=upower.service',
    ),
    Preset(
        "boot",
        "lifecycle",
        lambda e, boot: (
            bool(boot)
            and e.boot_id == boot
            and ((e.priority <= 3 and not e.kernel) or e.identifier == "systemd-modules-load")
        ),
        "-b 0 -p err + _SYSTEMD_UNIT=systemd-modules-load.service",
    ),
    Preset(
        "packages",
        "lifecycle",
        lambda e, _b: e.identifier in ("dnf", "apt", "apt-get", "dpkg", "pacman", "packagekitd", "zypper", "rpm"),
        "_COMM=dnf + _COMM=apt + _COMM=dpkg + _COMM=pacman + _SYSTEMD_UNIT=packagekit.service",
    ),
)

PRESETS_BY_ID = {p.id: p for p in PRESETS}


# -- problems ---------------------------------------------------------------


@dataclass
class Issue:
    """A problem found in the journal, with the entries that show it.

    ``kind`` says what was found: ``crash``, ``oom``, ``ssh``, ``storage``,
    ``unit-failed``, ``errors`` or ``warnings``. ``names`` holds what the
    explanation mentions (programs, addresses, a failure result).
    """

    id: str
    kind: str
    severity: str
    source: str  # unit or program the entries come from
    unit: str = ""  # service to open, if any
    names: list[str] = field(default_factory=list)
    entries: list[LogEntry] = field(default_factory=list)

    @property
    def latest(self) -> LogEntry:
        return self.entries[0]


_COREDUMP_RE = re.compile(r"Process \d+ \(([^)]+)\)")
_SEGFAULT_RE = re.compile(r"^(\S+?)\[\d+\]: (segfault|general protection|trap)")
_KILLED_RE = re.compile(r"Killed process \d+ \(([^)]+)\)")
_OOMD_RE = re.compile(r"Killed (\S+) due to")
_SSH_FAIL = _any(
    r"Failed password|Invalid user|authentication failure|maximum authentication attempts"
    r"|Connection closed by invalid user"
)
_SSH_ADDR_RE = re.compile(r"from (\S+) port")
_UNIT_FAILED_RE = re.compile(r"^(\S+\.service): Failed with result '([^']+)'")
# Numbers that change between otherwise identical messages: PIDs, durations, addresses.
_NUMBERS_RE = re.compile(r"0x[0-9a-f]+|\d+", re.IGNORECASE)

# Warnings seen this often from one source, with the same text, are flagged.
REPEATED_WARNINGS = 3


def _first_match(regex: re.Pattern, text: str) -> str:
    match = regex.search(text)
    return match.group(1) if match else ""


def _unique(values) -> list[str]:
    return list(dict.fromkeys(v for v in values if v))


def _source(entry: LogEntry) -> str:
    """Where an entry comes from. Instances of a template ("foo@1.service") count as one source, "foo"."""
    unit = entry.unit
    if "@" in unit:
        return unit.partition("@")[0]
    return unit or entry.identifier or "kernel"


def _classify(entry: LogEntry) -> tuple[str, str, str] | None:
    """(issue id, kind, severity) for entries that show a problem."""
    message = entry.message
    if entry.identifier == "systemd-coredump" and "dumped core" in message:
        return "crash", "crash", ERROR
    if entry.kernel and _CRASH(message):
        return "crash", "crash", ERROR
    if (entry.kernel and _OOM(message)) or (entry.identifier == "systemd-oomd" and "Killed" in message):
        return "oom", "oom", ERROR
    if entry.identifier in ("sshd", "sshd-session") and _SSH_FAIL(message):
        return "ssh", "ssh", WARNING
    if (entry.kernel and _STORAGE(message)) or (entry.identifier == "smartd" and entry.priority <= 4):
        return "storage", "storage", ERROR
    if entry.identifier in ("systemd", "init") and entry.unit.endswith(".service"):
        if _UNIT_FAILED_RE.search(message) or (entry.priority <= 3 and "Failed" in message):
            return f"unit:{entry.unit}", "unit-failed", ERROR
    if entry.priority <= 3:
        return f"errors:{_source(entry)}", "errors", ERROR
    return None


def find_issues(entries: list[LogEntry]) -> tuple[list[Issue], dict[int, Issue]]:
    """Group entries (newest first) into problems.

    Returns the problems, most severe and most recent first, and the
    problem each flagged entry belongs to, keyed by its index in ``entries``.
    """
    issues: dict[str, Issue] = {}
    flagged: dict[int, Issue] = {}

    def add(index: int, issue_id: str, kind: str, severity: str) -> None:
        entry = entries[index]
        issue = issues.get(issue_id)
        if issue is None:
            issue = issues[issue_id] = Issue(issue_id, kind, severity, _source(entry))
        issue.entries.append(entry)
        flagged[index] = issue

    warnings: dict[tuple[str, str], list[int]] = {}
    for index, entry in enumerate(entries):
        found = _classify(entry)
        if found:
            add(index, *found)
        elif entry.priority == 4:
            key = (_source(entry), _NUMBERS_RE.sub("#", entry.message))
            warnings.setdefault(key, []).append(index)

    for (source, _text), indexes in warnings.items():
        if len(indexes) >= REPEATED_WARNINGS:
            for index in indexes:
                add(index, f"warnings:{source}", "warnings", WARNING)

    for issue in issues.values():
        _describe(issue)

    order = sorted(issues.values(), key=lambda i: (i.severity != ERROR, -_newest(i)))
    return order, flagged


def _newest(issue: Issue) -> float:
    stamp = issue.latest.timestamp
    return stamp.timestamp() if stamp else 0.0


def _describe(issue: Issue) -> None:
    messages = [e.message for e in issue.entries]
    if issue.kind == "crash":
        issue.names = _unique(_first_match(_COREDUMP_RE, m) or _first_match(_SEGFAULT_RE, m) for m in messages)
        issue.source = (
            "systemd-coredump" if any(e.identifier == "systemd-coredump" for e in issue.entries) else "kernel"
        )
    elif issue.kind == "oom":
        issue.names = _unique(_first_match(_KILLED_RE, m) or _first_match(_OOMD_RE, m) for m in messages)
        issue.source = "kernel"
    elif issue.kind == "ssh":
        issue.names = _unique(_first_match(_SSH_ADDR_RE, m) for m in messages)
    elif issue.kind == "storage":
        issue.source = "kernel"
    elif issue.kind == "unit-failed":
        issue.unit = issue.source
        issue.names = _unique(match.group(2) for match in map(_UNIT_FAILED_RE.search, messages) if match)

    # Problems that come from a service can link to it. Crashes and the
    # kernel's reports name the program, not a service to look at.
    if not issue.unit and issue.kind in ("ssh", "errors", "warnings"):
        units = Counter(e.unit for e in issue.entries if e.unit.endswith(".service") and "@" not in e.unit)
        if units:
            issue.unit = units.most_common(1)[0][0]
