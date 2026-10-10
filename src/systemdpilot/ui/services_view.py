"""The services page: filters, then services grouped by state or as a table."""

from __future__ import annotations

from gettext import gettext as _

from gi.repository import Adw, Gio, GLib, GObject, Gtk, Pango

from ..core.models import Scope, Unit, UnitAction
from . import widgets, words
from .unit_list import UnitList
from .widgets import Chip, Option, OptionButton

SCOPE_ICONS = {Scope.SYSTEM: "network-server-symbolic", Scope.USER: "computer-symbolic"}


def mode_switch() -> Gtk.Widget:
    """Simple / Advanced, bound to ``win.mode``."""
    box = Gtk.Box(
        css_classes=["linked", "mode-switch"], valign=Gtk.Align.CENTER, tooltip_text=_("How much detail to show")
    )
    for value, text in (("simple", _("Simple")), ("advanced", _("Advanced"))):
        box.append(Gtk.ToggleButton(label=text, action_name="win.mode", action_target=GLib.Variant("s", value)))
    return box


class UnitRow(Gtk.ListBoxRow):
    """A service in the simple view. Start/stop buttons show while hovered or focused,
    and the switch enables or disables it."""

    def __init__(self, unit: Unit, view: ServicesView):
        super().__init__(activatable=True)
        self.unit = unit
        box = Gtk.Box(spacing=14, margin_top=12, margin_bottom=12, margin_start=16, margin_end=16)
        box.append(widgets.dot(unit.kind))

        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1, hexpand=True, valign=Gtk.Align.CENTER)
        title = words.unit_title(unit)
        text.append(widgets.label(title, "unit-title", ellipsize=Pango.EllipsizeMode.END))
        text.append(widgets.label(words.unit_subtitle(unit), "dim-label", "caption", ellipsize=Pango.EllipsizeMode.END))
        box.append(text)

        actions = Gtk.Box(spacing=4, valign=Gtk.Align.CENTER)
        if unit.kind == "running":
            buttons = (
                (UnitAction.RESTART, _("Restart"), "view-refresh-symbolic"),
                (UnitAction.STOP, _("Stop"), "media-playback-stop-symbolic"),
            )
        else:
            buttons = ((UnitAction.START, _("Start"), "media-playback-start-symbolic"),)
        for action, tooltip, icon in buttons:
            button = Gtk.Button(icon_name=icon, tooltip_text=tooltip, css_classes=["circular", "row-action"])
            button.connect("clicked", lambda _b, a=action: view.emit("unit-action", self.unit, a.value))
            actions.append(button)
        self._actions = Gtk.Revealer(
            child=actions, transition_type=Gtk.RevealerTransitionType.CROSSFADE, transition_duration=100
        )
        box.append(self._actions)

        state = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1, width_request=110, valign=Gtk.Align.CENTER)
        state.append(widgets.label(words.state_word(unit), "state-word", words.state_css(unit), xalign=1))
        state.append(widgets.label(words.boot_text(unit.file_state), "dim-label", "caption", xalign=1))
        box.append(state)

        # Same width with or without a switch, so the columns line up.
        enable = Gtk.Box(width_request=52, halign=Gtk.Align.END, valign=Gtk.Align.CENTER)
        if words.can_toggle_startup(unit):
            enabled = words.starts_at_boot(unit)
            switch = Gtk.Switch(
                active=enabled,
                valign=Gtk.Align.CENTER,
                tooltip_text=_("Disable (don’t start at boot)") if enabled else _("Enable (start at boot)"),
            )
            switch.update_property([Gtk.AccessibleProperty.LABEL], [_("Enabled")])
            switch.connect("state-set", self._on_enable_set, view)
            enable.append(switch)
        box.append(enable)
        box.append(Gtk.Image(icon_name="go-next-symbolic", css_classes=["dim-label"]))
        self.set_child(box)
        self.update_property([Gtk.AccessibleProperty.LABEL], [f"{title}, {words.state_word(unit)}"])

        self._hovered = self._focused = False
        motion = Gtk.EventControllerMotion()
        motion.connect("enter", lambda *_: self._set_hover(hovered=True))
        motion.connect("leave", lambda *_: self._set_hover(hovered=False))
        self.add_controller(motion)
        focus = Gtk.EventControllerFocus()
        focus.connect("enter", lambda *_: self._set_hover(focused=True))
        focus.connect("leave", lambda *_: self._set_hover(focused=False))
        self.add_controller(focus)

    def _on_enable_set(self, _switch, state, view):
        if state != words.starts_at_boot(self.unit):
            view.emit("unit-action", self.unit, (UnitAction.ENABLE if state else UnitAction.DISABLE).value)
        # Leave the switch pending; the list is rebuilt once the action has finished.
        return True

    def _set_hover(self, hovered: bool | None = None, focused: bool | None = None):
        if hovered is not None:
            self._hovered = hovered
        if focused is not None:
            self._focused = focused
        self._actions.set_reveal_child(self._hovered or self._focused)


class ServicesView(Gtk.Box):
    """Emits ``unit-activated`` (unit) and ``unit-action`` (unit, UnitAction value)."""

    __gtype_name__ = "SystemdPilotServicesView"
    __gsignals__ = {
        "unit-activated": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        "unit-action": (GObject.SignalFlags.RUN_FIRST, None, (object, str)),
        "filter-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    FILTERS = (
        ("all", _("All"), None),
        ("failed", _("Needs attention"), "failed"),
        ("running", _("Running"), "running"),
        ("exited", _("Done"), "exited"),
        ("dead", _("Stopped"), "dead"),
    )
    GROUPS = (
        ("failed", _("Needs attention"), _("These services stopped with an error"), "error"),
        ("running", _("Running"), _("Working in the background right now"), None),
        ("exited", _("Done"), _("Ran once, then finished"), None),
        ("dead", _("Stopped"), _("Not running"), None),
    )

    def __init__(self, unit_menu: Gio.MenuModel):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self._units: list[Unit] = []
        self._query = ""
        self._mode = "simple"
        self._dirty = False
        self._empty_hint = ""

        actions = Gio.SimpleActionGroup()
        self._filter = Gio.SimpleAction.new_stateful("filter", GLib.VariantType.new("s"), GLib.Variant("s", "all"))
        self._filter.connect("change-state", self._on_filter)
        actions.add_action(self._filter)
        self.insert_action_group("services", actions)

        bar = Gtk.Box(spacing=6, margin_top=12, margin_bottom=4, margin_start=24, margin_end=24)
        self.scope_button = OptionButton(
            "win.scope",
            [
                (
                    None,
                    [
                        Option("system", _("System services"), _("Shared by everyone, most start at boot"), "--system"),
                        Option("user", _("User services"), _("Only yours, start when you log in"), "--user"),
                    ],
                )
            ],
        )
        self.scope_button.set_tooltip_text(_("Which services to show"))
        self.scope_button.set_valign(Gtk.Align.START)
        bar.append(self.scope_button)
        bar.append(
            Gtk.Separator(
                orientation=Gtk.Orientation.VERTICAL,
                margin_top=6,
                margin_bottom=6,
                valign=Gtk.Align.START,
                height_request=20,
            )
        )
        chips = Gtk.Box(spacing=6, hexpand=True)
        self._chips: dict[str, Chip] = {}
        for value, text, dot_kind in self.FILTERS:
            chip = Chip(text, "services.filter", value, dot_kind)
            self._chips[value] = chip
            chips.append(chip)
        bar.append(chips)
        switch = mode_switch()
        switch.set_valign(Gtk.Align.START)
        bar.append(switch)
        self.append(widgets.scroller(bar))

        self.stack = Gtk.Stack(vexpand=True, hhomogeneous=False, transition_type=Gtk.StackTransitionType.CROSSFADE)
        self._groups = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=22, margin_top=8, margin_bottom=24)
        clamp = Adw.Clamp(
            child=self._groups, maximum_size=880, tightening_threshold=600, margin_start=24, margin_end=24
        )
        self._simple_scroll = Gtk.ScrolledWindow(child=clamp, hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.stack.add_named(self._simple_scroll, "simple")

        self.unit_list = UnitList(unit_menu)
        self.unit_list.connect("unit-activated", lambda _l, unit: self.emit("unit-activated", unit))
        card = Gtk.Box(
            css_classes=["card", "table-card"], margin_top=8, margin_bottom=24, margin_start=24, margin_end=24
        )
        card.append(self.unit_list)
        self.unit_list.set_hexpand(True)
        self.stack.add_named(card, "advanced")

        self.empty_page = Adw.StatusPage(icon_name="edit-find-symbolic")
        self.stack.add_named(self.empty_page, "empty")
        self.append(self.stack)

    # -- public API -------------------------------------------------------

    def set_units(self, units: list[Unit]) -> None:
        self._units = units
        self.unit_list.set_units(units)
        self._refresh()

    def clear(self) -> None:
        self._units = []
        self.unit_list.clear()
        widgets.clear(self._groups)

    @property
    def units(self) -> list[Unit]:
        return self._units

    @property
    def failed_count(self) -> int:
        return sum(1 for u in self._units if u.is_failed)

    @property
    def visible_count(self) -> int:
        return len(self._visible())

    def set_query(self, query: str) -> None:
        self._query = query.strip().lower()
        self.unit_list.set_query(query)
        self._refresh()

    def set_mode(self, mode: str) -> None:
        self._mode = mode
        self._refresh()

    def set_scope(self, scope: Scope) -> None:
        self.scope_button.set_text(_("User") if scope is Scope.USER else _("System"), SCOPE_ICONS[scope])

    def set_empty_hint(self, hint: str) -> None:
        self._empty_hint = hint

    @property
    def kind_filter(self) -> str:
        return self._filter.get_state().get_string()

    # -- internals --------------------------------------------------------

    def _on_filter(self, action, value):
        action.set_state(value)
        self.unit_list.set_kind(value.get_string())
        self._refresh()
        self.emit("filter-changed")

    def _matching(self) -> list[Unit]:
        q = self._query
        return [u for u in self._units if not q or q in u.name.lower() or q in u.description.lower()]

    def _visible(self) -> list[Unit]:
        kind = self.kind_filter
        return [u for u in self._matching() if kind == "all" or u.kind == kind]

    def _refresh(self) -> None:
        matching = self._matching()
        for value, chip in self._chips.items():
            chip.set_count(len(matching) if value == "all" else sum(1 for u in matching if u.kind == value))

        visible = self._visible()
        if not visible:
            self._show_empty()
            return
        if self._mode == "advanced":
            self.stack.set_visible_child_name("advanced")
            return
        self._rebuild_groups(visible)
        self.stack.set_visible_child_name("simple")

    def _show_empty(self) -> None:
        if self._query:
            self.empty_page.set_title(_("No Results Found"))
            self.empty_page.set_description(GLib.markup_escape_text(widgets.no_results(self._query)))
        else:
            self.empty_page.set_title(_("No Services Found"))
            self.empty_page.set_description(GLib.markup_escape_text(self._empty_hint))
        self.stack.set_visible_child_name("empty")

    def _rebuild_groups(self, visible: list[Unit]) -> None:
        adjustment = self._simple_scroll.get_vadjustment()
        position = adjustment.get_value()
        focused = self._focused_unit_name()
        widgets.clear(self._groups)
        ordered = sorted(visible, key=lambda u: u.name.lower())
        focus_row = None
        for kind, title, hint, css in self.GROUPS:
            units = [u for u in ordered if u.kind == kind]
            if not units:
                continue
            section = widgets.Section(title, hint, title_css=css)
            section.list.connect("row-activated", lambda _l, row: self.emit("unit-activated", row.unit))
            for unit in units:
                row = UnitRow(unit, self)
                section.list.append(row)
                if unit.name == focused:
                    focus_row = row
            self._groups.append(section.box)
        # Keep the place in the list when it is refreshed after an action.
        GLib.idle_add(lambda: adjustment.set_value(position) and False)
        if focus_row:
            focus_row.grab_focus()

    def _focused_unit_name(self) -> str | None:
        root = self.get_root()
        focus = root.get_focus() if root else None
        while focus is not None and not isinstance(focus, UnitRow):
            focus = focus.get_parent()
        return focus.unit.name if focus else None
