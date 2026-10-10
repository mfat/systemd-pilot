"""Plain-language words for systemd states, shared by the simple views."""

from __future__ import annotations

from datetime import datetime
from gettext import gettext as _
from gettext import ngettext

from ..core.models import Unit

# File states that "systemctl enable/disable" can change.
FIXED_FILE_STATES = ("", "static", "masked", "masked-runtime", "generated", "transient", "indirect", "alias")


def can_toggle_startup(unit: Unit) -> bool:
    return unit.file_state is not None and unit.file_state not in FIXED_FILE_STATES


def starts_at_boot(unit: Unit) -> bool:
    return (unit.file_state or "").startswith("enabled")


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
    if unit.active_state == "activating":
        return _("Starting")
    if unit.active_state == "deactivating":
        return _("Stopping")
    if unit.active_state == "reloading":
        return _("Reloading")
    return {
        "failed": _("Failed"),
        "running": _("Running"),
        "exited": _("Done"),
        "dead": _("Stopped"),
    }[unit.kind]


def state_css(unit: Unit) -> str:
    """Text color class for the state."""
    if unit.active_state in ("activating", "deactivating", "reloading"):
        return "warning"
    return {"failed": "error", "running": "success", "exited": "accent", "dead": "dim-label"}[unit.kind]


def boot_text(file_state: str | None) -> str:
    if file_state is None:
        return ""
    if file_state.startswith("enabled"):
        return _("Enabled")
    return {
        "static": _("Started when needed"),
        "disabled": _("Disabled"),
        "masked": _("Blocked"),
        "masked-runtime": _("Blocked"),
        "indirect": _("Started by other services"),
        "generated": _("Generated"),
        "transient": _("Temporary"),
        "alias": _("Alias"),
        "": _("No unit file"),
    }.get(file_state, file_state)


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


def ago(when: datetime | None, now: datetime | None = None) -> str:
    if when is None:
        return ""
    clock = now or datetime.now()
    if (clock - when).total_seconds() < 60:
        return _("just now")
    # Translators: {time} is a duration such as "5 minutes"
    return _("{time} ago").format(time=duration(when, clock))


def unit_subtitle(unit: Unit, order: str = "name-description") -> str:
    """Status line under the title in the services list (no uptime — that was costly)."""
    kind = unit.kind
    if unit.active_state == "activating":
        text = _("Starting up")
    elif kind == "running":
        text = _("Running")
    elif kind == "exited":
        text = _("Ran and finished")
    elif kind == "failed":
        text = _("Stopped with an error")
    else:
        text = _("Not running")
    detail = unit_description(unit) if order == "name-description" else unit.short_name
    return f"{text} · {detail}" if detail else text


def state_sentence(unit: Unit, now: datetime | None = None) -> str:
    """Follows the state word: "Running · started 3 hours ago"."""
    kind = unit.kind
    clock = now or datetime.now()
    if kind == "running" and unit.since:
        return _("started {ago} ({date})").format(
            ago=ago(unit.since, clock), date=unit.since.strftime("%a, %b %-d at %H:%M")
        )
    if kind == "running":
        return _("working in the background")
    if kind == "exited":
        return _("ran and finished normally")
    if kind == "failed" and unit.since:
        return _("stopped with an error {ago}").format(ago=ago(unit.since, clock))
    if kind == "failed":
        return _("stopped with an error")
    return _("not running right now")


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
