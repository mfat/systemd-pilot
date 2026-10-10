"""The services page: filters, then services grouped by state or as a table."""

from __future__ import annotations

from gettext import gettext as _

from gi.repository import Adw, Gio, GLib, GObject, Gtk, Pango

from ..core.models import Scope, Unit, UnitAction
from . import widgets, words
from .unit_list import UnitList
from .widgets import Option, OptionButton

SIMPLE_CHUNK = 25  # service rows built per idle tick on first paint


def mode_switch() -> Gtk.Widget:
    """Simple / Advanced, bound to ``win.mode``."""
    box = Gtk.Box(
        css_classes=["linked", "mode-switch"], valign=Gtk.Align.CENTER, tooltip_text=_("How much detail to show")
    )
    for value, text in (("simple", _("Simple")), ("advanced", _("Advanced"))):
        box.append(Gtk.ToggleButton(label=text, action_name="win.mode", action_target=GLib.Variant("s", value)))
    return box


class FilterRow(Gtk.ListBoxRow):
    """A state filter in the window sidebar: dot, name and how many services match."""

    def __init__(self, value: str, text: str, dot_kind: str | None):
        super().__init__()
        self.value = value
        box = Gtk.Box(spacing=12, margin_top=6, margin_bottom=6, margin_start=6, margin_end=6)
        if dot_kind:
            mark = Gtk.Box(width_request=16, valign=Gtk.Align.CENTER)  # dots line up with the icon
            mark.append(widgets.dot(dot_kind, small=True))
        else:
            mark = Gtk.Image(icon_name="view-list-symbolic")
        box.append(mark)
        box.append(widgets.label(text, hexpand=True, ellipsize=Pango.EllipsizeMode.END))
        self.count = widgets.label("", "dim-label", "numeric")
        box.append(self.count)
        self.set_child(box)
        self.update_property([Gtk.AccessibleProperty.LABEL], [text])

    def set_count(self, count: int) -> None:
        self.count.set_label(str(count))


class UnitRow(Gtk.ListBoxRow):
    """A service in the simple view. Start/stop buttons show while hovered or focused.

    Enable/disable lives in the details dialog — fetching unit-file state for every
    row was the main load bottleneck.
    """

    def __init__(self, unit: Unit, view: ServicesView):
        super().__init__(activatable=True)
        self.unit = unit
        self._view = view
        box = Gtk.Box(spacing=14, margin_top=12, margin_bottom=12, margin_start=16, margin_end=16)
        self._dot = widgets.dot(unit.kind)
        box.append(self._dot)

        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1, hexpand=True, valign=Gtk.Align.CENTER)
        order = view.label_order
        self._title = widgets.label(words.unit_title(unit, order), "unit-title", ellipsize=Pango.EllipsizeMode.END)
        self._subtitle = widgets.label(
            words.unit_subtitle(unit, order), "dim-label", "caption", ellipsize=Pango.EllipsizeMode.END
        )
        text.append(self._title)
        text.append(self._subtitle)
        box.append(text)

        self._action_box = Gtk.Box(spacing=4, valign=Gtk.Align.CENTER)
        self._fill_actions()
        self._actions = Gtk.Revealer(
            child=self._action_box, transition_type=Gtk.RevealerTransitionType.CROSSFADE, transition_duration=100
        )
        box.append(self._actions)

        self._state = widgets.label(
            words.state_word(unit),
            "state-word",
            words.state_css(unit),
            xalign=1,
            valign=Gtk.Align.CENTER,
            width_request=90,
        )
        box.append(self._state)
        box.append(Gtk.Image(icon_name="go-next-symbolic", css_classes=["dim-label"]))
        self.set_child(box)
        self._sync_a11y()

        self._hovered = self._focused = False
        motion = Gtk.EventControllerMotion()
        motion.connect("enter", lambda *_: self._set_hover(hovered=True))
        motion.connect("leave", lambda *_: self._set_hover(hovered=False))
        self.add_controller(motion)
        focus = Gtk.EventControllerFocus()
        focus.connect("enter", lambda *_: self._set_hover(focused=True))
        focus.connect("leave", lambda *_: self._set_hover(focused=False))
        self.add_controller(focus)

    def update(self, unit: Unit) -> None:
        """Refresh labels and controls without rebuilding the row widget tree."""
        old = self.unit
        if (
            unit.kind == old.kind
            and unit.active_state == old.active_state
            and unit.description == old.description
            and unit.name == old.name
        ):
            self.unit = unit
            return
        kind_changed = unit.kind != old.kind
        self.unit = unit
        if kind_changed:
            widgets.set_dot(self._dot, unit.kind)
            self._fill_actions()
        self._refresh_labels()
        self._state.set_label(words.state_word(unit))
        self._state.set_css_classes(["state-word", words.state_css(unit)])

    def _refresh_labels(self) -> None:
        order = self._view.label_order
        self._title.set_label(words.unit_title(self.unit, order))
        self._subtitle.set_label(words.unit_subtitle(self.unit, order))
        self._sync_a11y()

    def _fill_actions(self) -> None:
        widgets.clear(self._action_box)
        if self.unit.kind == "running":
            buttons = (
                (UnitAction.RESTART, _("Restart"), "view-refresh-symbolic"),
                (UnitAction.STOP, _("Stop"), "media-playback-stop-symbolic"),
            )
        else:
            buttons = ((UnitAction.START, _("Start"), "media-playback-start-symbolic"),)
        for action, tooltip, icon in buttons:
            button = Gtk.Button(icon_name=icon, tooltip_text=tooltip, css_classes=["circular", "row-action"])
            button.connect("clicked", lambda _b, a=action: self._view.emit("unit-action", self.unit, a.value))
            self._action_box.append(button)

    def _sync_a11y(self) -> None:
        self.update_property(
            [Gtk.AccessibleProperty.LABEL],
            [f"{words.unit_title(self.unit, self._view.label_order)}, {words.state_word(self.unit)}"],
        )

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
        self._label_order = "name-description"
        self._structure: tuple | None = None  # (kind, unit names…) of the built simple list
        self._build_gen = 0
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
        bar.append(Gtk.Box(hexpand=True))
        switch = mode_switch()
        switch.set_valign(Gtk.Align.START)
        bar.append(switch)
        self.append(widgets.scroller(bar))

        # The state filters live in the window sidebar.
        self.filter_list = Gtk.ListBox(css_classes=["navigation-sidebar"])
        self._filter_rows: dict[str, FilterRow] = {}
        for value, text, dot_kind in self.FILTERS:
            row = FilterRow(value, text, dot_kind)
            self._filter_rows[value] = row
            self.filter_list.append(row)
        self.filter_list.select_row(self._filter_rows["all"])
        self.filter_list.connect("row-selected", self._on_filter_row_selected)

        self.stack = Gtk.Stack(vexpand=True, hhomogeneous=False, transition_type=Gtk.StackTransitionType.CROSSFADE)
        self._groups = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=22,
            margin_top=8,
            margin_bottom=24,
            margin_start=24,
            margin_end=24,
            hexpand=True,
        )
        self._simple_scroll = Gtk.ScrolledWindow(child=self._groups, hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.stack.add_named(self._simple_scroll, "simple")

        self.unit_list = UnitList(unit_menu)
        self.unit_list.connect("unit-activated", lambda _l, unit: self.emit("unit-activated", unit))
        card = Gtk.Box(
            css_classes=["card", "table-card"],
            margin_top=8,
            margin_bottom=24,
            margin_start=24,
            margin_end=24,
            hexpand=True,
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
        if self._mode == "advanced":
            self.unit_list.set_units(units)
        self._refresh()

    def clear(self) -> None:
        self._build_gen += 1
        self._units = []
        self._structure = None
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
        if mode == self._mode:
            return
        self._mode = mode
        if mode == "advanced":
            self.unit_list.set_units(self._units)
        self._refresh()

    @property
    def label_order(self) -> str:
        return self._label_order

    def set_label_order(self, order: str) -> None:
        if order == self._label_order:
            return
        self._label_order = order
        self._refresh_row_labels()

    def set_scope(self, scope: Scope) -> None:
        self.scope_button.set_text(_("User") if scope is Scope.USER else _("System"))

    def set_empty_hint(self, hint: str) -> None:
        self._empty_hint = hint

    @property
    def kind_filter(self) -> str:
        return self._filter.get_state().get_string()

    # -- internals --------------------------------------------------------

    def _on_filter_row_selected(self, _listbox, row):
        if row is not None and row.value != self.kind_filter:
            self._filter.change_state(GLib.Variant("s", row.value))

    def _on_filter(self, action, value):
        action.set_state(value)
        self.filter_list.select_row(self._filter_rows[value.get_string()])
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
        for value, row in self._filter_rows.items():
            row.set_count(len(matching) if value == "all" else sum(1 for u in matching if u.kind == value))

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
        ordered = sorted(visible, key=lambda u: u.name.lower())
        plan: list[tuple[str, str, str, str | None, list[Unit]]] = []
        for kind, title, hint, css in self.GROUPS:
            units = [u for u in ordered if u.kind == kind]
            if units:
                plan.append((kind, title, hint, css, units))
        structure = tuple((kind, tuple(u.name for u in units)) for kind, _t, _h, _c, units in plan)
        # Same services in the same groups: update labels in place (common after
        # start/stop or a background refresh).
        if structure == self._structure and self._update_rows(plan):
            return

        adjustment = self._simple_scroll.get_vadjustment()
        position = adjustment.get_value()
        focused = self._focused_unit_name()
        # Reuse row widgets when units move between groups (start/stop/filter).
        existing = self._take_rows()
        self._build_gen += 1
        widgets.clear(self._groups)
        self._structure = structure
        row_count = sum(len(units) for *_rest, units in plan)
        if row_count > SIMPLE_CHUNK and not existing:
            self._rebuild_groups_chunked(plan, adjustment, position, focused, gen=self._build_gen)
            return
        focus_row = self._fill_groups(plan, existing, focused)
        GLib.idle_add(lambda: adjustment.set_value(position) and False)
        if focus_row:
            focus_row.grab_focus()

    def _fill_groups(
        self,
        plan: list[tuple[str, str, str, str | None, list[Unit]]],
        existing: dict[str, UnitRow],
        focused: str | None,
    ) -> UnitRow | None:
        focus_row = None
        for _kind, title, hint, css, units in plan:
            section = widgets.Section(title, hint, title_css=css)
            section.list.connect("row-activated", lambda _l, row: self.emit("unit-activated", row.unit))
            for unit in units:
                row = existing.pop(unit.name, None)
                if row is None:
                    row = UnitRow(unit, self)
                elif unit != row.unit:
                    row.update(unit)
                section.list.append(row)
                if unit.name == focused:
                    focus_row = row
            self._groups.append(section.box)
        return focus_row

    def _rebuild_groups_chunked(
        self,
        plan: list[tuple[str, str, str, str | None, list[Unit]]],
        adjustment,
        position: float,
        focused: str | None,
        *,
        gen: int,
    ) -> None:
        """First paint: add service rows in small batches so the window stays responsive."""
        state = {"section_idx": 0, "unit_idx": 0, "section": None, "focus_row": None}

        def append_unit(section: widgets.Section, unit: Unit) -> UnitRow:
            row = UnitRow(unit, self)
            section.list.append(row)
            return row

        def add_chunk():
            if gen != self._build_gen:
                return GLib.SOURCE_REMOVE
            added = 0
            while state["section_idx"] < len(plan) and added < SIMPLE_CHUNK:
                _kind, title, hint, css, units = plan[state["section_idx"]]
                if state["section"] is None:
                    section = widgets.Section(title, hint, title_css=css)
                    section.list.connect("row-activated", lambda _l, row: self.emit("unit-activated", row.unit))
                    self._groups.append(section.box)
                    state["section"] = section
                section = state["section"]
                while state["unit_idx"] < len(units) and added < SIMPLE_CHUNK:
                    unit = units[state["unit_idx"]]
                    row = append_unit(section, unit)
                    if unit.name == focused:
                        state["focus_row"] = row
                    state["unit_idx"] += 1
                    added += 1
                if state["unit_idx"] >= len(units):
                    state["section_idx"] += 1
                    state["unit_idx"] = 0
                    state["section"] = None
            if state["section_idx"] < len(plan):
                return GLib.SOURCE_CONTINUE
            GLib.idle_add(lambda: adjustment.set_value(position) and False)
            if state["focus_row"]:
                state["focus_row"].grab_focus()
            return GLib.SOURCE_REMOVE

        GLib.idle_add(add_chunk)

    def _take_rows(self) -> dict[str, UnitRow]:
        """Detach existing service rows so they can be re-parented into a new layout."""
        rows: dict[str, UnitRow] = {}
        section_box = self._groups.get_first_child()
        while section_box is not None:
            listbox = self._section_list(section_box)
            if listbox is not None:
                row = listbox.get_first_child()
                while row is not None:
                    nxt = row.get_next_sibling()
                    if isinstance(row, UnitRow):
                        listbox.remove(row)
                        rows[row.unit.name] = row
                    row = nxt
            section_box = section_box.get_next_sibling()
        return rows

    def _update_rows(self, plan: list[tuple[str, str, str, str | None, list[Unit]]]) -> bool:
        """Update existing rows when the group/name layout still matches. False → rebuild."""
        section_box = self._groups.get_first_child()
        for _kind, _title, _hint, _css, units in plan:
            if section_box is None:
                return False
            listbox = self._section_list(section_box)
            if listbox is None:
                return False
            row = listbox.get_first_child()
            for unit in units:
                if not isinstance(row, UnitRow) or row.unit.name != unit.name:
                    return False
                if unit != row.unit:
                    row.update(unit)
                row = row.get_next_sibling()
            if row is not None:
                return False
            section_box = section_box.get_next_sibling()
        return section_box is None

    @staticmethod
    def _section_list(section_box: Gtk.Widget) -> Gtk.ListBox | None:
        child = section_box.get_first_child()
        while child is not None:
            if isinstance(child, Gtk.ListBox):
                return child
            child = child.get_next_sibling()
        return None

    def _focused_unit_name(self) -> str | None:
        root = self.get_root()
        focus = root.get_focus() if root else None
        while focus is not None and not isinstance(focus, UnitRow):
            focus = focus.get_parent()
        return focus.unit.name if focus else None

    def _refresh_row_labels(self) -> None:
        section_box = self._groups.get_first_child()
        while section_box is not None:
            listbox = self._section_list(section_box)
            if listbox is not None:
                row = listbox.get_first_child()
                while row is not None:
                    if isinstance(row, UnitRow):
                        row._refresh_labels()
                    row = row.get_next_sibling()
            section_box = section_box.get_next_sibling()
