"""Fill text buffers with highlighted logs and properties.

Text goes in through :class:`Gtk.TextTag`s rather than Pango markup, so
nothing in a log line or property value can be interpreted as markup.
"""

from __future__ import annotations

from gi.repository import Gtk, Pango

from ..core.models import LogEntry

_TAGS = {
    "dim": {"foreground": "#8a8a8a"},
    "key": {"weight": Pango.Weight.BOLD},
    "error": {"foreground": "#e62d42", "weight": Pango.Weight.BOLD},
    "warning": {"foreground": "#c88800"},
    "notice": {"foreground": "#3584e4"},
}


def _ensure_tags(buffer: Gtk.TextBuffer) -> None:
    table = buffer.get_tag_table()
    for name, props in _TAGS.items():
        if table.lookup(name) is None:
            buffer.create_tag(name, **props)


def _priority_tag(priority: int) -> str | None:
    if priority <= 3:
        return "error"
    if priority == 4:
        return "warning"
    if priority == 5:
        return "notice"
    if priority >= 7:
        return "dim"
    return None


def set_logs(buffer: Gtk.TextBuffer, entries: list[LogEntry]) -> None:
    buffer.set_text("")
    _ensure_tags(buffer)
    end = buffer.get_end_iter()
    for entry in entries:
        stamp = entry.timestamp.strftime("%b %d %H:%M:%S") if entry.timestamp else "—"
        buffer.insert_with_tags_by_name(end, stamp + " ", "dim")
        source = entry.identifier + (f"[{entry.pid}]" if entry.pid else "")
        if source:
            buffer.insert_with_tags_by_name(end, source + ": ", "dim")
        tag = _priority_tag(entry.priority)
        if tag:
            buffer.insert_with_tags_by_name(end, entry.message, tag)
        else:
            buffer.insert(end, entry.message)
        buffer.insert(end, "\n")


def set_properties(buffer: Gtk.TextBuffer, properties: dict[str, str], query: str = "") -> None:
    buffer.set_text("")
    _ensure_tags(buffer)
    end = buffer.get_end_iter()
    query = query.strip().lower()
    for key in sorted(properties):
        value = properties[key]
        if query and query not in key.lower() and query not in value.lower():
            continue
        buffer.insert_with_tags_by_name(end, key, "key")
        buffer.insert_with_tags_by_name(end, "=", "dim")
        buffer.insert(end, value + "\n")
