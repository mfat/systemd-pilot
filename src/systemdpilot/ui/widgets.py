"""Small widgets shared by the services view, the journal view and the unit dialog."""

from __future__ import annotations

from dataclasses import dataclass

from gi.repository import GLib, Gtk, Pango

from ..core.models import LogEntry
from ..i18n import _
from . import words


def dot(kind: str, *, small: bool = False) -> Gtk.Widget:
    """A colored circle. ``kind`` is a status-dot class from style.css."""
    widget = Gtk.Box(valign=Gtk.Align.CENTER, halign=Gtk.Align.CENTER)
    set_dot(widget, kind, small=small)
    return widget


def set_dot(widget: Gtk.Widget, kind: str, *, small: bool = False) -> None:
    widget.set_css_classes(["status-dot", kind] + (["small"] if small else []))


def label(text: str = "", *css: str, **props) -> Gtk.Label:
    props.setdefault("xalign", 0)
    if props.get("wrap"):
        # Unit names, paths and addresses are long words; let them break anywhere.
        props.setdefault("wrap_mode", Pango.WrapMode.WORD_CHAR)
    return Gtk.Label(label=text, css_classes=list(css), **props)


def count_badge(css: str = "error") -> Gtk.Label:
    return Gtk.Label(css_classes=["count-badge", css], valign=Gtk.Align.CENTER, visible=False)


def set_count_badge(badge: Gtk.Label, count: int) -> None:
    badge.set_label(str(count))
    badge.set_visible(count > 0)


class Chip(Gtk.ToggleButton):
    """A filter pill: optional dot, label and a count. Bound to a string action."""

    def __init__(self, text: str, action_name: str, value: str, dot_kind: str | None = None):
        super().__init__(action_name=action_name, action_target=GLib.Variant("s", value), css_classes=["chip"])
        box = Gtk.Box(spacing=7)
        if dot_kind:
            box.append(dot(dot_kind, small=True))
        box.append(Gtk.Label(label=text))
        self.count = Gtk.Label(css_classes=["chip-count"])
        box.append(self.count)
        self.set_child(box)

    def set_count(self, count: int) -> None:
        self.count.set_label(str(count))


@dataclass(frozen=True)
class Option:
    value: str
    label: str
    help: str = ""
    flag: str = ""  # the matching command-line option, shown dimmed


class OptionButton(Gtk.MenuButton):
    """A pill that opens a list of options for a string action.

    ``groups`` is a list of (heading, options); a heading may be None.
    """

    def __init__(
        self,
        action_name: str,
        groups: list[tuple[str | None, list[Option]]],
        *,
        intro: str = "",
        width: int = 300,
        counts: bool = False,
        css: tuple[str, ...] = ("chip",),
    ):
        super().__init__(css_classes=list(css), always_show_arrow=True)
        self._label = Gtk.Label()
        self._icon = Gtk.Image(visible=False)
        content = Gtk.Box(spacing=7)
        content.append(self._icon)
        content.append(self._label)
        self.set_child(content)
        self._counts: dict[str, Gtk.Label] = {}
        self._titles: dict[str, Gtk.Label] = {}

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, width_request=width)
        if intro:
            box.append(label(intro, "dim-label", "caption", wrap=True, margin_start=10, margin_end=10, margin_top=4))
        for heading, options in groups:
            if heading:
                box.append(label(heading.upper(), "option-heading"))
            for option in options:
                box.append(self._option(action_name, option, counts))

        scroll = Gtk.ScrolledWindow(
            child=box, hscrollbar_policy=Gtk.PolicyType.NEVER, propagate_natural_height=True, max_content_height=470
        )
        popover = Gtk.Popover(child=scroll, css_classes=["options"])
        self.set_popover(popover)

    def _option(self, action_name: str, option: Option, counts: bool) -> Gtk.Widget:
        button = Gtk.ToggleButton(
            action_name=action_name, action_target=GLib.Variant("s", option.value), css_classes=["flat", "option"]
        )
        row = Gtk.Box(spacing=12)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1, hexpand=True)
        title = label(option.label, wrap=True)
        self._titles[option.value] = title
        text.append(title)
        if option.help:
            text.append(label(option.help, "dim-label", "caption", wrap=True))
        row.append(text)
        if option.flag:
            row.append(label(option.flag, "dim-label", "monospace", "caption", valign=Gtk.Align.CENTER))
        if counts:
            count = Gtk.Label(css_classes=["option-count"], valign=Gtk.Align.CENTER)
            self._counts[option.value] = count
            row.append(count)
        button.set_child(row)
        button.connect("clicked", lambda *_: self.popdown())
        return button

    def set_text(self, text: str, icon_name: str | None = None) -> None:
        self._label.set_label(text)
        self._icon.set_visible(bool(icon_name))
        if icon_name:
            self._icon.set_from_icon_name(icon_name)

    def set_option_count(self, value: str, count: int) -> None:
        badge = self._counts.get(value)
        if badge:
            badge.set_label(str(count))
            badge.set_css_classes(["option-count"] + ([] if count else ["empty"]))
            self._titles[value].set_css_classes([] if count else ["dim-label"])


class Section:
    """A heading and a hint over a boxed list, as in the design's grouped lists."""

    def __init__(self, title: str = "", hint: str = "", *, title_css: str | None = None):
        self.box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        header = Gtk.Box(spacing=8, margin_start=4, margin_end=4)
        self.heading = label(title, "heading")
        if title_css:
            self.heading.add_css_class(title_css)
        header.append(self.heading)
        self.hint = label(hint, "dim-label", "caption", ellipsize=Pango.EllipsizeMode.END, valign=Gtk.Align.END)
        header.append(self.hint)
        self.box.append(header)
        self.list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, css_classes=["boxed-list"])
        self.box.append(self.list)


def log_row(entry: LogEntry, *, show_source: bool = True, badge: str = "", badge_css: str = "") -> Gtk.ListBoxRow:
    """One journal entry: time, a dot for its level, the message and where it came from."""
    grid = Gtk.Box(spacing=12, margin_top=10, margin_bottom=10, margin_start=16, margin_end=16)
    when = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, width_request=52, valign=Gtk.Align.START)
    if entry.timestamp:
        when.append(label(entry.timestamp.strftime("%H:%M"), "dim-label", "numeric"))
        when.append(label(entry.timestamp.strftime("%b %d"), "dim-label", "caption"))
    else:
        when.append(label("—", "dim-label"))
    grid.append(when)
    level_word, level_css = words.level(entry.priority)
    level_dot = dot(words.level_dot(entry.priority), small=True)
    level_dot.set_valign(Gtk.Align.START)
    level_dot.set_margin_top(6)  # level with the first line of the message
    grid.append(level_dot)
    text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True)
    # Crash reports and the like run for many lines; the first one says what happened.
    first, _sep, rest = entry.message.partition("\n")
    message = label(first + (" …" if rest.strip() else ""), wrap=True, selectable=True)
    if rest.strip():
        message.set_tooltip_text(entry.message[:2000])
    if level_css:
        message.add_css_class(level_css)
    text.append(message)
    source = entry.identifier + (f"[{entry.pid}]" if entry.pid else "")
    meta = f"{source} · {level_word}" if show_source and source else level_word
    text.append(label(meta, "dim-label", "caption", ellipsize=Pango.EllipsizeMode.END))
    grid.append(text)
    if badge:
        grid.append(
            Gtk.Label(
                label=badge,
                css_classes=["badge", badge_css],
                valign=Gtk.Align.START,
                ellipsize=Pango.EllipsizeMode.END,
                max_width_chars=16,
                tooltip_text=badge,
            )
        )
    row = Gtk.ListBoxRow(child=grid, activatable=False)
    if badge:
        row.add_css_class(f"flagged-{badge_css}")
    return row


def scroller(child: Gtk.Widget) -> Gtk.ScrolledWindow:
    """Lets a toolbar scroll sideways when the window is too narrow for it."""
    return Gtk.ScrolledWindow(
        child=child,
        vscrollbar_policy=Gtk.PolicyType.NEVER,
        propagate_natural_height=True,
        propagate_natural_width=False,
    )


def placeholder_row(text: str) -> Gtk.ListBoxRow:
    return Gtk.ListBoxRow(
        child=label(text, "dim-label", margin_top=12, margin_bottom=12, margin_start=16, margin_end=16),
        activatable=False,
    )


def clear(listbox: Gtk.ListBox | Gtk.Box) -> None:
    while child := listbox.get_first_child():
        listbox.remove(child)


def no_results(query: str) -> str:
    return _("Nothing matches “{query}”.").format(query=query)
