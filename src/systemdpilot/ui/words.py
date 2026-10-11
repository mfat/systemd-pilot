"""Wording for systemd states: systemd terms first; short captions where useful."""

from __future__ import annotations

import re
from datetime import datetime

from ..core.models import Unit, UnitAction
from ..core.parsers import parse_unit_list
from ..i18n import _, ngettext

# An icon for each unit type, in the sidebar and the details header.
TYPE_ICONS = {
    "service": "cogged-wheel-symbolic",
    "socket": "network-wired-symbolic",
    "timer": "alarm-symbolic",
    "path": "folder-symbolic",
    "target": "find-location-symbolic",
    "mount": "drive-harddisk-symbolic",
    "automount": "drive-removable-media-symbolic",
    "swap": "drive-multidisk-symbolic",
    "device": "computer-symbolic",
    "slice": "view-grid-symbolic",
    "scope": "applications-system-symbolic",
}

# File states that "systemctl enable/disable" cannot change.
FIXED_FILE_STATES = ("", "static", "masked", "masked-runtime", "generated", "transient", "indirect", "alias")
# No unit file / generated / already masked: masking is not useful.
_UNMASKABLE = frozenset({"masked", "masked-runtime"})
_NOT_MASKABLE = frozenset({"", "masked", "masked-runtime", "generated", "transient", "alias"})


# The root file system and slice, and PID 1's scope, stay up as long as the machine does.
_UNSTOPPABLE = frozenset({"-.mount", "-.slice", "init.scope"})


def control_actions(unit: Unit, props: dict[str, str] | None = None) -> tuple[UnitAction, ...]:
    """Start, or restart and stop, as far as the unit allows.

    Devices come and go with the hardware; scopes are made by the programs in
    them, so they can only be stopped. With the unit's ``props``, systemd's own
    CanStart/CanStop and RefuseManualStart/RefuseManualStop decide too.
    """
    running = unit.kind == "running"
    if unit.type == "device" or unit.name in _UNSTOPPABLE:
        return ()
    if unit.type == "scope":
        actions: tuple[UnitAction, ...] = (UnitAction.STOP,) if running else ()
    else:
        actions = (UnitAction.RESTART, UnitAction.STOP) if running else (UnitAction.START,)
    if props:
        can_start = props.get("CanStart") != "no" and props.get("RefuseManualStart") != "yes"
        can_stop = props.get("CanStop") != "no" and props.get("RefuseManualStop") != "yes"
        allowed = {UnitAction.START: can_start, UnitAction.STOP: can_stop, UnitAction.RESTART: can_start and can_stop}
        actions = tuple(a for a in actions if allowed[a])
    return actions


def detail_actions(unit: Unit, props: dict[str, str] | None = None) -> tuple[UnitAction, ...]:
    """:func:`control_actions`, with Reload after Restart for a running unit that can reload."""
    actions = control_actions(unit, props)
    if UnitAction.RESTART in actions and can_reload(props):
        i = actions.index(UnitAction.RESTART) + 1
        return (*actions[:i], UnitAction.RELOAD, *actions[i:])
    return actions


def can_reload(props: dict[str, str] | None) -> bool:
    return bool(props) and props.get("CanReload") == "yes"


def confirm_control(unit: Unit, action: UnitAction) -> tuple[str, str, str] | None:
    """(heading, body, button) to ask before ``action``, or None to go ahead.

    Stopping a target can take down the units that need it.
    """
    if unit.type != "target" or action not in (UnitAction.STOP, UnitAction.RESTART):
        return None
    body = _("Units that need this target may stop with it.")
    if action is UnitAction.STOP:
        return _("Stop {unit}?").format(unit=unit.short_name), body, _("S_top")
    return _("Restart {unit}?").format(unit=unit.short_name), body, _("_Restart")


def can_signal(unit: Unit, props: dict[str, str]) -> bool:
    """The unit has processes running in a control group of its own."""
    return unit.kind == "running" and bool(props.get("ControlGroup")) and unit.name not in _UNSTOPPABLE


# Types named by the software that ships them, not made up by the machine
# (devices, mounts, sessions), so the web knows about them.
_SEARCHABLE_TYPES = ("service", "socket", "timer", "path", "target")
_MAN_RE = re.compile(r"man:([^()\s]+)(?:\((\w+)\))?$")
MAN_PAGES_URL = "https://manpages.debian.org/{page}"


def search_name(unit: Unit) -> str:
    """What to search the web for: the unit's name, its template's for an instance, or ""."""
    if unit.type not in _SEARCHABLE_TYPES:
        return ""
    base, _dot, kind = unit.name.rpartition(".")
    prefix, at, _instance = base.partition("@")
    return f"{prefix}@.{kind}" if at else unit.name


def documentation(props: dict[str, str]) -> list[tuple[str, str]]:
    """The unit's ``Documentation=`` links it can open, as (title, URI): man pages and web pages."""
    links = []
    for uri in parse_unit_list(props.get("Documentation", "")):
        if match := _MAN_RE.match(uri):
            name, section = match.groups()
            links.append((f"{name}({section})" if section else name, uri))
        elif uri.startswith(("https://", "http://")):
            links.append((uri.split("://", 1)[1].rstrip("/"), uri))
    return links


def man_page_url(uri: str) -> str:
    """A web address for a ``man:`` link, for machines with no manual viewer."""
    name, section = _MAN_RE.match(uri).groups()
    return MAN_PAGES_URL.format(page=f"{name}.{section}" if section else name)


def can_override(unit: Unit, props: dict[str, str]) -> bool:
    """The unit comes from a file of its own, so a drop-in can change it."""
    path = props.get("FragmentPath") or ""
    return bool(path) and not path.startswith("/run/systemd/transient/") and unit.type not in ("device", "scope")


def override_template(unit: Unit) -> str:
    """What a new override starts as: a note on how drop-ins work, and the type's own section."""
    section = {"target": "Unit"}.get(unit.type, unit.type.capitalize())
    note = _(
        "# Settings here are added to {unit} and override its own.\n"
        "# To replace a list such as ExecStart=, clear it first with an empty ExecStart=.\n"
        "# Saving with no settings removes the override.\n"
    ).format(unit=unit.name)
    return f"{note}\n[{section}]\n"


def can_toggle_startup(unit: Unit) -> bool:
    return unit.file_state is not None and unit.file_state not in FIXED_FILE_STATES


def can_mask(unit: Unit) -> bool:
    return unit.file_state is not None and unit.file_state not in _NOT_MASKABLE


def can_unmask(unit: Unit) -> bool:
    return (unit.file_state or "") in _UNMASKABLE


def starts_at_boot(unit: Unit) -> bool:
    return (unit.file_state or "").startswith("enabled")


def enable_caption(unit: Unit) -> str:
    """Subtitle for the Enabled switch when it is on."""
    return _("starts at login") if unit.is_user else _("starts at boot")


def unit_description(unit: Unit) -> str:
    """A real description, or empty if systemd just echoed the unit name."""
    description = unit.description.strip()
    if not description or description in (unit.name, unit.short_name):
        return ""
    return description


def unit_title(unit: Unit, order: str = "name-description") -> str:
    """Primary label in the services list."""
    if order == "name-description":
        return unit.short_name
    return unit_description(unit) or unit.short_name


def state_word(unit: Unit) -> str:
    """systemd ActiveState while changing, otherwise the SubState-based kind."""
    if unit.active_state in ("activating", "deactivating", "reloading"):
        return unit.active_state
    # Only services run: a timer is "waiting", a device "plugged", a mount "mounted".
    if unit.kind == "running" and unit.type != "service":
        return unit.sub_state or unit.active_state
    return unit.kind


def state_caption(unit: Unit) -> str:
    """Optional short caption; empty when the systemd term is enough."""
    if unit.active_state in ("activating", "deactivating", "reloading"):
        return ""
    return {
        "exited": "active (exited)",
        "failed": "failed",
        "dead": "inactive",
    }.get(unit.kind, "")


def state_css(unit: Unit) -> str:
    """Text color class for the state."""
    if unit.active_state in ("activating", "deactivating", "reloading"):
        return "warning"
    return {"failed": "error", "running": "success", "exited": "accent", "dead": "dim-label"}[unit.kind]


def file_state_word(file_state: str | None) -> str:
    """systemd's UnitFileState (enabled, static, masked, …)."""
    if file_state is None:
        return ""
    return file_state or "—"


def file_state_caption(file_state: str | None) -> str:
    """Short caption for a UnitFileState; empty when the term is enough."""
    if file_state is None:
        return ""
    if file_state.startswith("enabled"):
        return _("starts at boot")
    return {
        "static": "no [Install]",
        "masked": "→ /dev/null",
        "masked-runtime": "→ /dev/null (runtime)",
        "indirect": "indirect",
        "generated": "generated",
        "transient": "transient",
        "alias": "alias",
        "": "no unit file",
    }.get(file_state, "")


def file_state_tooltip(file_state: str | None) -> str:
    """Tooltip when a longer note helps; empty otherwise."""
    if file_state in ("static", "indirect"):
        return "no [Install] — started by dependencies or activation units"
    if file_state in ("masked", "masked-runtime"):
        return "systemctl unmask to allow start"
    return ""


def duration(since: datetime, now: datetime | None = None) -> str:
    """How long ago, as "5 minutes", "3 hours" or "2 days"."""
    seconds = max(0, int(((now or datetime.now()) - since).total_seconds()))
    if seconds < 60:
        return _("less than a minute")
    minutes = seconds // 60
    if minutes < 60:
        return ngettext("{n} minute", "{n} minutes", minutes).format(n=minutes)
    hours = round(minutes / 60)
    if minutes < 48 * 60:
        return ngettext("{n} hour", "{n} hours", hours).format(n=hours)
    days = round(minutes / 1440)
    return ngettext("{n} day", "{n} days", days).format(n=days)


def moment(when: datetime, now: datetime | None = None) -> str:
    """A time before or after now: "in 5 hours · Mon, Oct 12 at 00:00", "2 days ago · …"."""
    clock = now or datetime.now()
    date = when.strftime("%a, %b %-d at %H:%M")
    if when > clock:
        # Translators: {time} is a duration such as "5 minutes"
        return _("in {time} · {date}").format(time=duration(clock, when), date=date)
    return _("{ago} · {date}").format(ago=ago(when, clock), date=date)


def ago(when: datetime | None, now: datetime | None = None) -> str:
    if when is None:
        return ""
    clock = now or datetime.now()
    if (clock - when).total_seconds() < 60:
        return _("just now")
    # Translators: {time} is a duration such as "5 minutes"
    return _("{time} ago").format(time=duration(when, clock))


def unit_subtitle(unit: Unit, order: str = "name-description") -> str:
    """Secondary label under the title (status is shown separately on the row)."""
    if order == "name-description":
        return unit_description(unit)
    return unit.short_name


def state_sentence(unit: Unit, now: datetime | None = None) -> str:
    """Caption under Active: since-time when known, else a short systemd-oriented note."""
    kind = unit.kind
    clock = now or datetime.now()
    if kind == "running" and unit.since:
        return _("since {ago} · {date}").format(
            ago=ago(unit.since, clock), date=unit.since.strftime("%a, %b %-d at %H:%M")
        )
    if kind == "running":
        return ""
    if kind == "exited":
        return "active (exited)"
    if kind == "failed" and unit.since:
        return _("failed {ago}").format(ago=ago(unit.since, clock))
    if kind == "failed":
        return "failed"
    return "inactive"


def size(memory: int | None) -> str:
    if not memory:
        return "—"
    mb = memory / 1024 / 1024
    if mb >= 1024:
        return _("{n} GB").format(n=f"{mb / 1024:.1f}")
    return _("{n} MB").format(n=f"{mb:.1f}" if mb < 10 else f"{mb:.0f}")


def cpu_time(nanoseconds: int | None) -> str:
    if nanoseconds is None:
        return "—"
    seconds = nanoseconds / 1e9
    if seconds < 1:
        return _("under a second")
    if seconds < 60:
        return ngettext("{n} second", "{n} seconds", round(seconds)).format(n=f"{seconds:.1f}".rstrip("0").rstrip("."))
    minutes = seconds / 60
    if minutes < 60:
        return ngettext("{n} minute", "{n} minutes", round(minutes)).format(n=round(minutes))
    hours = minutes / 60
    return ngettext("{n} hour", "{n} hours", round(hours)).format(n=f"{hours:.1f}".rstrip("0").rstrip("."))


def level(priority: int) -> tuple[str, str]:
    """(word, text css class) for a syslog priority."""
    if priority <= 3:
        return _("Error"), "error"
    if priority == 4:
        return _("Warning"), "warning"
    if priority == 5:
        return _("Notice"), ""
    if priority >= 7:
        return _("Debug"), "dim-label"
    return _("Info"), ""


def level_dot(priority: int) -> str:
    """Status dot class for a syslog priority."""
    if priority <= 3:
        return "failed"
    if priority == 4:
        return "warning"
    if priority == 5:
        return "notice"
    return "info"
