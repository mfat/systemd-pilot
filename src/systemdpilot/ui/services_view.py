"""The services page: filters, then services grouped by state."""

from __future__ import annotations

from gi.repository import Adw, Gio, GLib, GObject, Gtk, Pango

from ..core.models import Unit, UnitAction
from ..i18n import _
from . import widgets, words
from .widgets import FilterRow


def matches_filter(unit: Unit, value: str) -> bool:
    """Whether the sidebar filter ``value`` ("all", "user" or a :attr:`Unit.kind`) shows ``unit``."""
    if value == "all":
        return True
    if value == "user":
        return unit.is_user
    return unit.kind == value


def user_tag() -> Gtk.Widget:
    """Marks a user service: run by the user's own systemd, not the machine's."""
    return widgets.label(
        _("User"),
        "badge",
        "user",
        "compact",
        valign=Gtk.Align.CENTER,
        tooltip_text=_("User-level systemd unit"),
    )


GROUP_ORDER = {"failed": 0, "running": 1, "exited": 2, "dead": 3}
# New services added to the simple list at a time, more than a screenful: each
# row the list view makes for them takes a while.
FILL_CHUNK = 40
ACTIONS_WIDTH = (32, 68)  # start; or restart and stop: 32px buttons 4px apart


class ServiceItem(GObject.Object):
    """A service in the simple list. Emits ``changed`` when its unit is replaced."""

    __gtype_name__ = "SystemdPilotServiceItem"
    __gsignals__ = {"changed": (GObject.SignalFlags.RUN_FIRST, None, ())}

    def __init__(self, unit: Unit):
        super().__init__()
        self.unit = unit


class GroupItem(GObject.Object):
    """The heading of a group in the simple list, sorted ahead of its services."""

    __gtype_name__ = "SystemdPilotGroupItem"

    def __init__(self, kind: str):
        super().__init__()
        self.kind = kind


class SectionHeader(Gtk.Box):
    """The heading and hint over a group of services."""

    def __init__(self):
        super().__init__(spacing=8, margin_start=28, margin_end=28, margin_bottom=8)
        self._heading = widgets.label("", "heading")
        self.append(self._heading)
        self._hint = widgets.label("", "dim-label", "caption", ellipsize=Pango.EllipsizeMode.END, valign=Gtk.Align.END)
        self.append(self._hint)

    def show(self, kind: str, first: bool) -> None:
        _kind, title, hint, css = ServicesView.GROUPS[GROUP_ORDER[kind]]
        self._heading.set_label(title)
        self._heading.set_css_classes(["heading", css] if css else ["heading"])
        self._hint.set_label(hint)
        self.set_margin_top(8 if first else 22)


class ListRow(Adw.Bin):
    """One row of the simple list: a group heading or a service, whichever it is bound to.

    The list only has rows for what is on screen, and reuses them while scrolling.
    """

    def __init__(self, view: ServicesView, list_item: Gtk.ListItem):
        super().__init__()
        self._view = view
        self._list_item = list_item
        self.item: ServiceItem | GroupItem | None = None
        self._header: SectionHeader | None = None
        self.service: ServiceRow | None = None
        # The list's own row around this widget is what takes keyboard focus.
        self._focus = Gtk.EventControllerFocus()
        self._focus.connect("enter", lambda *_: self._set_focused(True))
        self._focus.connect("leave", lambda *_: self._set_focused(False))
        self._focus_target: Gtk.Widget | None = None
        self.connect("notify::parent", lambda *_: self._follow_parent())
        list_item.connect("notify::position", lambda *_: self.sync())

    def bind(self, item: ServiceItem | GroupItem) -> None:
        self.item = item
        is_service = isinstance(item, ServiceItem)
        self._list_item.set_activatable(is_service)
        self._list_item.set_selectable(is_service)
        self._list_item.set_focusable(is_service)
        if is_service:
            if self.service is None:
                self.service = ServiceRow(self._view, self._list_item)
            self.set_child(self.service)
            self.service.bind(item)
        else:
            if self._header is None:
                self._header = SectionHeader()
            self.set_child(self._header)
            self._list_item.set_accessible_label("")
        self.sync()

    def unbind(self) -> None:
        if isinstance(self.item, ServiceItem):
            self.service.unbind()
        self.item = None

    def sync(self) -> None:
        """Follow the row's place: the first heading has less space above it, and a
        group is drawn as one card with rounded corners on its first and last rows."""
        position = self._list_item.get_position()
        if self.item is None or position == Gtk.INVALID_LIST_POSITION:
            return
        if isinstance(self.item, GroupItem):
            self._header.show(self.item.kind, first=position == 0)
            return
        model = self._view.model
        before = model.get_item(position - 1) if position else None
        after = model.get_item(position + 1)
        self.service.set_edges(not isinstance(before, ServiceItem), not isinstance(after, ServiceItem))

    def _follow_parent(self) -> None:
        if self._focus_target is not None:
            self._focus_target.remove_controller(self._focus)
        self._focus_target = self.get_parent()
        if self._focus_target is not None:
            self._focus_target.add_controller(self._focus)

    def _set_focused(self, focused: bool) -> None:
        if self.service is not None:
            self.service.set_hover(focused=focused)


class ServiceRow(Gtk.Box):
    """A service in the simple view. Start/stop buttons show while hovered or focused.

    Enable/disable lives in the details dialog — fetching unit-file state for every
    row was the main load bottleneck.
    """

    def __init__(self, view: ServicesView, list_item: Gtk.ListItem):
        super().__init__(css_classes=["service-row"], margin_start=24, margin_end=24)
        self._view = view
        self._list_item = list_item
        self.item: ServiceItem | None = None
        self._changed_handler = 0
        self._actions_kind = ""
        # As in a boxed list: its rows add 2px around their content.
        box = Gtk.Box(spacing=14, margin_top=14, margin_bottom=14, margin_start=18, margin_end=18, hexpand=True)
        self._dot = widgets.dot("dead")
        box.append(self._dot)

        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1, hexpand=True, valign=Gtk.Align.CENTER)
        self._title = widgets.label("", "unit-title", ellipsize=Pango.EllipsizeMode.END, width_chars=6)
        self._subtitle = widgets.label("", "dim-label", "caption", ellipsize=Pango.EllipsizeMode.END)
        # The tag follows the name; a long name is cut short, never the tag.
        self._title_line = Gtk.Box(spacing=6)
        self._title_line.append(self._title)
        self._user_tag: Gtk.Widget | None = None  # made for the first user service shown
        text.append(self._title_line)
        text.append(self._subtitle)
        box.append(text)

        # Buttons are slow to make, and most rows are never hovered: they are made
        # when first shown. The box keeps their room, so names don't shift then.
        self._action_box = Gtk.Box(spacing=4, valign=Gtk.Align.CENTER)
        self._actions = Gtk.Revealer(
            child=self._action_box, transition_type=Gtk.RevealerTransitionType.CROSSFADE, transition_duration=100
        )
        box.append(self._actions)

        self._state = widgets.label("", "state-word", xalign=1, valign=Gtk.Align.CENTER, width_request=90)
        box.append(self._state)
        box.append(Gtk.Image(icon_name="go-next-symbolic", css_classes=["dim-label"]))
        self.append(box)

        self._hovered = self._focused = False
        motion = Gtk.EventControllerMotion()
        motion.connect("enter", lambda *_: self.set_hover(hovered=True))
        motion.connect("leave", lambda *_: self.set_hover(hovered=False))
        self.add_controller(motion)

    @property
    def unit(self) -> Unit | None:
        return self.item.unit if self.item else None

    def bind(self, item: ServiceItem) -> None:
        self.item = item
        self._changed_handler = item.connect("changed", lambda *_: self.show())
        self.show()

    def unbind(self) -> None:
        if self.item is not None:
            self.item.disconnect(self._changed_handler)
        self.item = None

    def show(self) -> None:
        unit = self.item.unit
        widgets.set_dot(self._dot, unit.kind)
        if unit.is_user and self._user_tag is None:
            self._user_tag = user_tag()
            self._title_line.append(self._user_tag)
        if self._user_tag is not None:
            self._user_tag.set_visible(unit.is_user)
        self.refresh_labels()
        self._state.set_label(words.state_word(unit))
        self._state.set_css_classes(["state-word", words.state_css(unit)])
        self._action_box.set_size_request(ACTIONS_WIDTH[unit.kind == "running"], -1)
        if self._actions.get_reveal_child():
            self._sync_actions()

    def refresh_labels(self) -> None:
        unit, order = self.item.unit, self._view.label_order
        self._title.set_label(words.unit_title(unit, order))
        self._subtitle.set_label(words.unit_subtitle(unit, order))
        self._list_item.set_accessible_label(f"{words.unit_title(unit, order)}, {words.state_word(unit)}")

    def set_edges(self, first: bool, last: bool) -> None:
        self.set_css_classes(["service-row"] + (["first"] if first else []) + (["last"] if last else []))

    def _sync_actions(self) -> None:
        kind = self.item.unit.kind if self.item else ""
        if kind and kind != self._actions_kind:
            self._fill_actions(kind)

    def _fill_actions(self, kind: str) -> None:
        self._actions_kind = kind
        widgets.clear(self._action_box)
        if kind == "running":
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

    def set_hover(self, hovered: bool | None = None, focused: bool | None = None):
        if hovered is not None:
            self._hovered = hovered
        if focused is not None:
            self._focused = focused
        reveal = self._hovered or self._focused
        if reveal:
            self._sync_actions()
        self._actions.set_reveal_child(reveal)


class ServicesView(Gtk.Box):
    """Emits ``unit-activated`` (unit) and ``unit-action`` (unit, UnitAction value)."""

    __gtype_name__ = "SystemdPilotServicesView"
    __gsignals__ = {
        "unit-activated": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        "unit-action": (GObject.SignalFlags.RUN_FIRST, None, (object, str)),
        "filter-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    FILTERS = (
        ("all", _("All services"), None),
        ("failed", _("Needs attention"), "failed"),
        ("running", _("Running"), "running"),
        ("exited", _("Done"), "exited"),
        ("dead", _("Stopped"), "dead"),
        # Not a state: services run by the user's own systemd, in whatever state.
        ("user", _("User services"), "user"),
    )
    GROUPS = (
        ("failed", _("Needs attention"), _("These services stopped with an error"), "error"),
        ("running", _("Running"), _("Working in the background right now"), None),
        ("exited", _("Done"), _("Ran once, then finished"), None),
        ("dead", _("Stopped"), _("Not running"), None),
    )

    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self._units: list[Unit] = []
        self._query = ""
        self._label_order = "name-description"
        self._empty_hint = ""

        actions = Gio.SimpleActionGroup()
        self._filter = Gio.SimpleAction.new_stateful("filter", GLib.VariantType.new("s"), GLib.Variant("s", "all"))
        self._filter.connect("change-state", self._on_filter)
        actions.add_action(self._filter)
        self.insert_action_group("services", actions)

        bar = Gtk.Box(spacing=6, margin_top=12, margin_bottom=4, margin_start=24, margin_end=24)
        # While services are still being fetched or filled in the background.
        self._busy_spinner = Gtk.Spinner(valign=Gtk.Align.CENTER)
        self._busy = Gtk.Box(spacing=8, visible=False, valign=Gtk.Align.CENTER)
        self._busy.append(self._busy_spinner)
        self._busy.append(widgets.label(_("Loading…"), "dim-label", "caption"))
        bar.append(self._busy)
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
        self.sidebar = self.filter_list

        self.stack = Gtk.Stack(vexpand=True, hhomogeneous=False, transition_type=Gtk.StackTransitionType.CROSSFADE)
        # Grouped by state, then by name, each group after its heading. A list view
        # only makes rows for what is on screen; a row for each of hundreds of
        # services took seconds to build.
        self._store = Gio.ListStore(item_type=GObject.Object)
        order = Gtk.CustomSorter.new(lambda a, b, _d: _compare(_sort_key(a), _sort_key(b)))
        self.model = Gtk.SortListModel(model=self._store, sorter=order)
        self._selection = Gtk.SingleSelection(model=self.model, autoselect=False, can_unselect=True)
        self._selected_unit: Unit | None = None  # open in the details panel; kept highlighted
        self._fill_source = 0  # adds the next new services to the list
        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", lambda _f, list_item: list_item.set_child(ListRow(self, list_item)))
        factory.connect("bind", lambda _f, list_item: list_item.get_child().bind(list_item.get_item()))
        factory.connect("unbind", lambda _f, list_item: list_item.get_child().unbind())
        self.simple_list = Gtk.ListView(
            model=self._selection,
            factory=factory,
            single_click_activate=True,
            css_classes=["services-list"],
        )
        self.simple_list.update_property([Gtk.AccessibleProperty.LABEL], [_("Services")])
        self.simple_list.connect("activate", self._on_row_activated)
        self._simple_scroll = Gtk.ScrolledWindow(child=self.simple_list, hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.stack.add_named(self._simple_scroll, "simple")

        self.empty_page = Adw.StatusPage(icon_name="edit-find-symbolic")
        self.stack.add_named(self.empty_page, "empty")
        self.append(self.stack)

    # -- public API -------------------------------------------------------

    def set_units(self, units: list[Unit]) -> None:
        self._units = units
        self._refresh()

    def set_busy(self, busy: bool) -> None:
        self._busy.set_visible(busy)
        self._busy_spinner.set_spinning(busy)

    def clear(self) -> None:
        self._units = []
        self._selected_unit = None
        if self._fill_source:
            GLib.source_remove(self._fill_source)
            self._fill_source = 0
        self._store.remove_all()

    def select_unit(self, unit: Unit | None) -> None:
        """Keep ``unit`` highlighted, or clear the highlight when details close."""
        self._selected_unit = unit
        self._apply_selection()

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
        self._refresh()

    @property
    def label_order(self) -> str:
        return self._label_order

    def set_label_order(self, order: str) -> None:
        if order == self._label_order:
            return
        self._label_order = order
        self._refresh_row_labels()

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
        self._refresh()
        if self.model.get_n_items():
            self.simple_list.scroll_to(0, Gtk.ListScrollFlags.NONE, None)
        self.emit("filter-changed")

    def _matching(self) -> list[Unit]:
        q = self._query
        return [u for u in self._units if not q or q in u.name.lower() or q in u.description.lower()]

    def _visible(self) -> list[Unit]:
        kind = self.kind_filter
        return [u for u in self._matching() if matches_filter(u, kind)]

    def _refresh(self) -> None:
        matching = self._matching()
        for value, row in self._filter_rows.items():
            row.set_count(sum(1 for u in matching if matches_filter(u, value)))

        visible = self._visible()
        if not visible:
            self._show_empty()
        else:
            self._update_simple(visible)
            self.stack.set_visible_child_name("simple")

    def _show_empty(self) -> None:
        if self._query:
            self.empty_page.set_title(_("No Results Found"))
            self.empty_page.set_description(GLib.markup_escape_text(widgets.no_results(self._query)))
        else:
            self.empty_page.set_title(_("No Services Found"))
            self.empty_page.set_description(GLib.markup_escape_text(self._empty_hint))
        self.stack.set_visible_child_name("empty")

    def _update_simple(self, visible: list[Unit]) -> None:
        """Change the simple list in place, so scrolling and focus are kept."""
        incoming = {u.key: u for u in visible}
        gone: list[int] = []
        moved: list[ServiceItem] = []
        kinds: set[str] = set()
        headings: dict[str, int] = {}
        # Services that leave the list or change group go; the sorted model puts
        # those that come back in their new place.
        for position in range(self._store.get_n_items()):
            item = self._store.get_item(position)
            if isinstance(item, GroupItem):
                headings[item.kind] = position
                continue
            unit = incoming.pop(item.unit.key, None)
            if unit is None:
                gone.append(position)
                continue
            if unit.kind != item.unit.kind:
                gone.append(position)
                item.unit = unit
                moved.append(item)
            elif unit != item.unit:
                item.unit = unit
                item.emit("changed")
            kinds.add(unit.kind)
        new = list(incoming.values())
        if len(new) > FILL_CHUNK:
            new = sorted(new, key=_unit_order)[:FILL_CHUNK]
            if not self._fill_source:
                self._fill_source = GLib.idle_add(self._fill_more)
        kinds.update(u.kind for u in new)
        # Headings of groups left empty go too.
        gone = sorted(gone + [position for kind, position in headings.items() if kind not in kinds])
        for start, count in reversed(_runs(gone)):
            self._store.splice(start, count, [])
        added = [GroupItem(kind) for kind in kinds - headings.keys()]
        added += moved + [ServiceItem(u) for u in new]
        if added:
            self._store.splice(self._store.get_n_items(), 0, added)
        if gone or added:
            # Rows whose neighbours changed, e.g. a group's new last service.
            for row in self._bound_rows():
                row.sync()
        self._apply_selection()

    def _fill_more(self) -> bool:
        self._fill_source = 0
        self._refresh()
        return GLib.SOURCE_REMOVE

    def _bound_rows(self):
        child = self.simple_list.get_first_child()
        while child is not None:
            row = child.get_first_child()
            if isinstance(row, ListRow) and row.item is not None:
                yield row
            child = child.get_next_sibling()

    def _on_row_activated(self, _view, position: int) -> None:
        item = self.model.get_item(position)
        if isinstance(item, ServiceItem):
            self._selected_unit = item.unit
            self.emit("unit-activated", item.unit)

    def _apply_selection(self) -> None:
        """Select the open service in the list model, if it is still shown."""
        target = Gtk.INVALID_LIST_POSITION
        unit = self._selected_unit
        if unit is not None:
            for position in range(self.model.get_n_items()):
                item = self.model.get_item(position)
                if isinstance(item, ServiceItem) and item.unit.key == unit.key:
                    target = position
                    self._selected_unit = item.unit  # the list's current object
                    break
        if self._selection.get_selected() != target:
            self._selection.set_selected(target)

    def _refresh_row_labels(self) -> None:
        for row in self._bound_rows():
            if isinstance(row.item, ServiceItem):
                row.service.refresh_labels()


def _compare(a, b) -> int:
    return (a > b) - (a < b)


def _sort_key(item: ServiceItem | GroupItem) -> tuple:
    if isinstance(item, GroupItem):
        return GROUP_ORDER[item.kind], False, "", False
    return _unit_order(item.unit)


def _unit_order(unit: Unit) -> tuple:
    return GROUP_ORDER[unit.kind], True, unit.name.lower(), unit.is_user


def _runs(positions: list[int]) -> list[tuple[int, int]]:
    """Ascending positions as (start, count) runs of neighbours."""
    runs: list[tuple[int, int]] = []
    for position in positions:
        if runs and runs[-1][0] + runs[-1][1] == position:
            runs[-1] = (runs[-1][0], runs[-1][1] + 1)
        else:
            runs.append((position, 1))
    return runs
