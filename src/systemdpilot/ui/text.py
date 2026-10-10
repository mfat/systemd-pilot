"""Fill text buffers with highlighted logs.

Text goes in through :class:`Gtk.TextTag`s rather than Pango markup, so
nothing in a log line can be interpreted as markup.
"""

from __future__ import annotations

import weakref

from gi.repository import Adw, Gtk, Pango

from ..core.models import LogEntry

# GNOME palette shades with enough contrast on the light and the dark background.
_COLORS = {
    False: {"dim": "#77767b", "error": "#c01c28", "warning": "#9c6e03", "notice": "#1c71d8"},
    True: {"dim": "#9a9996", "error": "#ff7b63", "warning": "#f8e45c", "notice": "#99c1f1"},
}


def _apply_colors(buffer: Gtk.TextBuffer, dark: bool) -> None:
    table = buffer.get_tag_table()
    for name, color in _COLORS[dark].items():
        table.lookup(name).set_property("foreground", color)


def _ensure_tags(buffer: Gtk.TextBuffer) -> None:
    table = buffer.get_tag_table()
    if table.lookup("key") is not None:
        return
    buffer.create_tag("key", weight=Pango.Weight.BOLD)
    buffer.create_tag("error", weight=Pango.Weight.BOLD)
    for name in ("dim", "warning", "notice"):
        buffer.create_tag(name)

    style = Adw.StyleManager.get_default()
    _apply_colors(buffer, style.get_dark())

    # Follow light/dark switches without keeping the buffer alive: the
    # handler holds only a weak reference, and is removed with the buffer.
    buffer._pilot_tags = True  # ties the Python wrapper to the GObject's lifetime
    ref = weakref.ref(buffer)

    def on_dark(manager, _pspec):
        if (live := ref()) is not None:
            _apply_colors(live, manager.get_dark())

    handler = style.connect("notify::dark", on_dark)
    buffer.weak_ref(lambda: style.disconnect(handler))


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


def _insert_entry(buffer: Gtk.TextBuffer, end: Gtk.TextIter, entry: LogEntry) -> None:
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


def set_logs(buffer: Gtk.TextBuffer, entries: list[LogEntry]) -> None:
    buffer.set_text("")
    _ensure_tags(buffer)
    end = buffer.get_end_iter()
    for entry in entries:
        _insert_entry(buffer, end, entry)
        buffer.insert(end, "\n")
