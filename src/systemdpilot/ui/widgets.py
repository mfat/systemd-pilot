"""Small widgets shared by the services view, the journal view and the unit dialog."""

from __future__ import annotations

from dataclasses import dataclass

from gi.repository import GLib, GObject, Gtk, Pango

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


@dataclass(frozen=True)
class Option:
    value: str
    label: str
    help: str = ""
    flag: str = ""  # the matching command-line option, shown dimmed
    short: str = ""  # on a closed dropdown, instead of the label


class OptionDropDown(Gtk.DropDown):
    """A dropdown of :class:`Option` values. The open list shows each option's help and flag too.

    Emits ``reselected`` (value) when the option already chosen is clicked in the list again.
    """

    __gtype_name__ = "SystemdPilotOptionDropDown"
    __gsignals__ = {"reselected": (GObject.SignalFlags.RUN_FIRST, None, (str,))}

    def __init__(self, options: list[Option], **props):
        super().__init__(model=Gtk.StringList.new([o.value for o in options]), **props)
        self.options = options

        # The closed button's label; an option can show other text there than in the list.
        self._button_text: dict[str, str] = {}
        self._button_labels: dict[Gtk.ListItem, Gtk.Label] = {}
        button = Gtk.SignalListItemFactory()
        # Cut short, so dropdowns side by side can share a narrow row.
        button.connect("setup", lambda _f, item: item.set_child(Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)))
        button.connect("bind", self._bind_button)
        button.connect("unbind", lambda _f, item: self._button_labels.pop(item, None))
        self.set_factory(button)

        # A custom list loses the dropdown's own check mark on the current choice, so rows draw one.
        self._checks: dict[Gtk.ListItem, Gtk.Image] = {}
        rows = Gtk.SignalListItemFactory()
        rows.connect("setup", self._setup_row)
        rows.connect("bind", self._bind_row)
        rows.connect("unbind", lambda _f, item: self._checks.pop(item, None))
        self.set_list_factory(rows)
        self.connect("notify::selected", lambda *_: self._sync_checks())

    @property
    def value(self) -> str:
        return self.options[self.get_selected()].value

    def set_value(self, value: str) -> None:
        index = next((i for i, o in enumerate(self.options) if o.value == value), 0)
        if index != self.get_selected():
            self.set_selected(index)

    def set_button_text(self, value: str, text: str = "") -> None:
        """Show ``text`` on the closed button while ``value`` is chosen; empty, its label."""
        self._button_text[value] = text
        for item, button_label in self._button_labels.items():
            self._show_button_text(item, button_label)

    def _bind_button(self, _factory, item: Gtk.ListItem) -> None:
        self._button_labels[item] = item.get_child()
        self._show_button_text(item, item.get_child())

    def _show_button_text(self, item: Gtk.ListItem, button_label: Gtk.Label) -> None:
        option = self._option(item)
        button_label.set_label(self._button_text.get(option.value) or option.short or option.label)

    def _option(self, item: Gtk.ListItem) -> Option:
        return self.options[item.get_position()]

    def _setup_row(self, _factory, item: Gtk.ListItem) -> None:
        row = Gtk.Box(spacing=12, margin_top=2, margin_bottom=2)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1, hexpand=True, valign=Gtk.Align.CENTER)
        text.append(label())
        text.append(label("", "dim-label", "caption"))
        row.append(text)
        row.append(label("", "dim-label", "monospace", "caption", valign=Gtk.Align.CENTER))
        row.append(Gtk.Image(icon_name="object-select-symbolic", valign=Gtk.Align.CENTER))
        # Choosing the current option again changes nothing, so the dropdown says nothing: tell.
        click = Gtk.GestureClick(propagation_phase=Gtk.PropagationPhase.CAPTURE)
        click.connect("pressed", lambda *_: self._on_row_pressed(item))
        row.add_controller(click)
        item.set_child(row)

    def _on_row_pressed(self, item: Gtk.ListItem) -> None:
        if item.get_position() == self.get_selected():
            value = self.value
            # After the list closes.
            GLib.idle_add(lambda: self.emit("reselected", value) and False)

    def _bind_row(self, _factory, item: Gtk.ListItem) -> None:
        option = self._option(item)
        text = item.get_child().get_first_child()
        title, help_label = text.get_first_child(), text.get_last_child()
        flag = text.get_next_sibling()
        self._checks[item] = item.get_child().get_last_child()
        self._sync_checks()
        title.set_label(option.label)
        help_label.set_label(option.help)
        help_label.set_visible(bool(option.help))
        flag.set_label(option.flag)
        flag.set_visible(bool(option.flag))

    def _sync_checks(self) -> None:
        selected = self.get_selected()
        for item, check in self._checks.items():
            check.set_opacity(1 if item.get_position() == selected else 0)


class FilterRow(Gtk.ListBoxRow):
    """A filter in the window sidebar: icon or dot, name and how many match."""

    def __init__(self, value: str, text: str, dot_kind: str | None = None, icon_name: str = "", **props):
        super().__init__(**props)
        self.value = value
        box = Gtk.Box(spacing=12, margin_top=6, margin_bottom=6, margin_start=6, margin_end=6)
        if dot_kind:
            mark = Gtk.Box(width_request=16, valign=Gtk.Align.CENTER)  # dots line up with the icons
            mark.append(dot(dot_kind, small=True))
        else:
            mark = Gtk.Image(icon_name=icon_name or "cogged-wheel-symbolic")
        box.append(mark)
        box.append(label(text, hexpand=True, ellipsize=Pango.EllipsizeMode.END))
        self.count = label("", "dim-label", "numeric")
        box.append(self.count)
        self.set_child(box)
        self.update_property([Gtk.AccessibleProperty.LABEL], [text])

    def set_count(self, count: int) -> None:
        self.count.set_label(str(count))


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


def log_row(entry: LogEntry, *, show_source: bool = True) -> Gtk.ListBoxRow:
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
    return Gtk.ListBoxRow(child=grid, activatable=False)


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
        selectable=False,
    )


def clear(listbox: Gtk.ListBox | Gtk.Box) -> None:
    while child := listbox.get_first_child():
        listbox.remove(child)


def no_results(query: str) -> str:
    return _("Nothing matches “{query}”.").format(query=query)
