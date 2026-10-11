"""The units page: filters, then units by name."""

from __future__ import annotations

from collections.abc import Callable

from gi.repository import Adw, Gio, GLib, GObject, Gtk, Pango

from ..core.models import Unit
from ..i18n import _
from . import widgets, words
from .widgets import FilterRow


def matches_scope(unit: Unit, scope: str) -> bool:
    """Whether ``unit`` matches a scope filter (``all``, ``system``, ``user``)."""
    if scope == "user":
        return unit.is_user
    if scope == "system":
        return not unit.is_user
    return True


# Sidebar filters past the states: units that are likely in trouble.
PRESET_MATCH = {
    # Crashed and waiting to be started again; kind still says "running".
    "auto-restart": lambda u: u.sub_state == "auto-restart",
    # Meant to start at boot, but not running now.
    "enabled-dead": lambda u: words.starts_at_boot(u) and u.kind == "dead",
    # Started by hand: gone after a reboot.
    "active-disabled": lambda u: u.is_active and (u.file_state or "").startswith("disabled"),
    "masked": lambda u: (u.file_state or "").startswith("masked"),
    "load-error": lambda u: u.load_state in ("not-found", "bad-setting", "error"),
}


def is_inactive_unit(unit: Unit) -> bool:
    """Units ``list-units`` hides without ``--all`` (inactive / not loaded)."""
    return unit.kind == "dead"


def matches_kind(unit: Unit, kind: str) -> bool:
    """Whether ``unit`` matches a state filter: ``all``, a :attr:`Unit.kind` or a preset."""
    if kind in PRESET_MATCH:
        return PRESET_MATCH[kind](unit)
    return kind == "all" or unit.kind == kind


def matches_type(unit: Unit, unit_type: str) -> bool:
    """Whether ``unit`` matches a type filter: ``all`` or a unit type such as ``timer``."""
    return unit_type == "all" or unit.type == unit_type


def matches_filter(unit: Unit, scope: str, kind: str, unit_type: str = "all", *, show_inactive: bool = True) -> bool:
    """Whether ``unit`` matches the scope, state and type sidebar filters.

    When ``show_inactive`` is false, inactive units stay out of the main list, but
    the ``dead`` filter and presets that look for idle/broken units still see them.
    """
    if not (matches_scope(unit, scope) and matches_kind(unit, kind) and matches_type(unit, unit_type)):
        return False
    if show_inactive or kind == "dead" or kind in PRESET_MATCH:
        return True
    return not is_inactive_unit(unit)


def _list_heading(title: str, *, first: bool = False) -> Callable:
    """A header func that puts ``title`` over a sidebar list's first row."""

    def header(row: Gtk.ListBoxRow, before: Gtk.ListBoxRow | None) -> None:
        if before is None and row.get_header() is None:
            margin_top = 6 if first else 12
            row.set_header(
                widgets.label(
                    title, "dim-label", "caption-heading", margin_top=margin_top, margin_bottom=6, margin_start=12
                )
            )

    return header


def user_tag() -> Gtk.Widget:
    """Marks a user service: run by the user's own systemd, not the machine's."""
    return widgets.label(
        "user",
        "badge",
        "user",
        "compact",
        valign=Gtk.Align.CENTER,
        tooltip_text="systemctl --user",
    )


# New services added to the simple list at a time, more than a screenful: each
# row the list view makes for them takes a while.
FILL_CHUNK = 40


class ServiceItem(GObject.Object):
    """A service in the simple list. Emits ``changed`` when its unit is replaced."""

    __gtype_name__ = "SystemdPilotServiceItem"
    __gsignals__ = {"changed": (GObject.SignalFlags.RUN_FIRST, None, ())}

    def __init__(self, unit: Unit):
        super().__init__()
        self.unit = unit


class ListRow(Adw.Bin):
    """One row of the simple list, bound to a unit.

    The list only has rows for what is on screen, and reuses them while scrolling.
    """

    def __init__(self, view: ServicesView, list_item: Gtk.ListItem):
        super().__init__()
        self._view = view
        self._list_item = list_item
        self.item: ServiceItem | None = None
        self.service: ServiceRow | None = None
        list_item.connect("notify::position", lambda *_: self.sync())

    def bind(self, item: ServiceItem) -> None:
        self.item = item
        if self.service is None:
            self.service = ServiceRow(self._view, self._list_item)
            self.set_child(self.service)
        self.service.bind(item)
        self.sync()

    def unbind(self) -> None:
        self.service.unbind()
        self.item = None

    def sync(self) -> None:
        """Follow the row's place: the list is drawn as one card with rounded corners
        on its first and last rows."""
        position = self._list_item.get_position()
        if self.item is None or position == Gtk.INVALID_LIST_POSITION:
            return
        self.service.set_edges(position == 0, position == self._view.model.get_n_items() - 1)


class ServiceRow(Gtk.Box):
    """A service in the simple view. Its controls live in the details panel.

    Enable/disable lives in the details dialog — fetching unit-file state for every
    row was the main load bottleneck.
    """

    def __init__(self, view: ServicesView, list_item: Gtk.ListItem):
        super().__init__(css_classes=["service-row"], margin_start=24, margin_end=24)
        self._view = view
        self._list_item = list_item
        self.item: ServiceItem | None = None
        self._changed_handler = 0
        self._edges: list[str] = []  # "first" and "last" of the list
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

        self._state = widgets.label("", "state-word", xalign=1, valign=Gtk.Align.CENTER, width_request=110)
        box.append(self._state)
        box.append(Gtk.Image(icon_name="systemdpilot-go-next-symbolic", css_classes=["dim-label"]))
        self.append(box)

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
        self._state.set_tooltip_text(words.state_caption(unit) or None)
        self._state.set_css_classes(["state-word", words.state_css(unit)])
        self.sync_classes()

    def refresh_labels(self) -> None:
        unit, order = self.item.unit, self._view.label_order
        self._title.set_label(words.unit_title(unit, order))
        self._subtitle.set_label(words.unit_subtitle(unit, order))
        self._list_item.set_accessible_label(f"{words.unit_title(unit, order)}, {words.state_word(unit)}")

    def set_edges(self, first: bool, last: bool) -> None:
        self._edges = (["first"] if first else []) + (["last"] if last else [])
        self.sync_classes()

    def sync_classes(self) -> None:
        """Its place in the list, and whether it is the service open in details."""
        is_open = self.item is not None and self._view.is_open(self.item.unit)
        self.set_css_classes(["service-row", *self._edges] + (["open"] if is_open else []))


class ServicesView(Gtk.Box):
    """Emits ``unit-activated`` (unit)."""

    __gtype_name__ = "SystemdPilotServicesView"
    __gsignals__ = {
        "unit-activated": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        "filter-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    # Scope and state are independent; "all" in each means no filtering.
    SCOPE_FILTERS = (
        ("all", "All", "systemctl --system + --user"),
        ("system", "System", "systemctl --system"),
        ("user", "User", "systemctl --user"),
    )
    KIND_FILTERS = (
        ("running", "running", "running", "active (running)"),
        ("failed", "failed", "failed", "failed"),
        ("exited", "exited", "exited", "active (exited)"),
        ("dead", "dead", "dead", "inactive (dead)"),
    )
    # value, label, dot, tooltip; in the same list as the states.
    PRESET_FILTERS = (
        ("auto-restart", "auto-restart", "warning", "activating (auto-restart): crashed, restarting"),
        ("enabled-dead", "enabled, inactive", "ring-warning", "UnitFileState=enabled, inactive (dead)"),
        ("active-disabled", "active, disabled", "ring-running", "UnitFileState=disabled: stops after reboot"),
        ("masked", "masked", "info", "UnitFileState=masked → /dev/null"),
        ("load-error", "load error", "ring-failed", "LoadState=not-found, bad-setting or error"),
    )
    # Last in the status list, as "all types" is in the type list.
    ANY_FILTER = ("all", "any", "any", "any state")
    # value, label, icon, tooltip
    TYPE_FILTERS = (
        ("service", "service", words.TYPE_ICONS["service"], "systemd.service(5): processes"),
        ("socket", "socket", words.TYPE_ICONS["socket"], "systemd.socket(5): starts a service on connection"),
        ("timer", "timer", words.TYPE_ICONS["timer"], "systemd.timer(5): starts a unit on a schedule"),
        ("path", "path", words.TYPE_ICONS["path"], "systemd.path(5): starts a unit when files change"),
        ("target", "target", words.TYPE_ICONS["target"], "systemd.target(5): groups units, as boot stages"),
        ("mount", "mount", words.TYPE_ICONS["mount"], "systemd.mount(5): file system mount points"),
        ("automount", "automount", words.TYPE_ICONS["automount"], "systemd.automount(5): mounts on first access"),
        ("swap", "swap", words.TYPE_ICONS["swap"], "systemd.swap(5): swap space"),
        ("device", "device", words.TYPE_ICONS["device"], "systemd.device(5): devices the kernel reports"),
        ("slice", "slice", words.TYPE_ICONS["slice"], "systemd.slice(5): resource groups"),
        ("scope", "scope", words.TYPE_ICONS["scope"], "systemd.scope(5): processes started elsewhere"),
        ("all", "all types", "systemdpilot-view-list-symbolic", "every unit type"),
    )

    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self._units: list[Unit] = []
        self._query = ""
        self._label_order = "name-description"
        self._empty_hint = ""
        self._show_inactive = False

        actions = Gio.SimpleActionGroup()
        self._scope = Gio.SimpleAction.new_stateful("scope", GLib.VariantType.new("s"), GLib.Variant("s", "all"))
        self._scope.connect("change-state", self._on_scope)
        actions.add_action(self._scope)
        self._filter = Gio.SimpleAction.new_stateful("filter", GLib.VariantType.new("s"), GLib.Variant("s", "running"))
        self._filter.connect("change-state", self._on_filter)
        actions.add_action(self._filter)
        # Services first, as before other types were listed: devices alone are hundreds.
        self._type = Gio.SimpleAction.new_stateful("type", GLib.VariantType.new("s"), GLib.Variant("s", "service"))
        self._type.connect("change-state", self._on_type)
        actions.add_action(self._type)
        self.insert_action_group("services", actions)

        bar = Gtk.Box(spacing=6, margin_top=12, margin_bottom=4, margin_start=24, margin_end=24)
        # While services are still being fetched or filled in the background.
        self._busy_spinner = Gtk.Spinner(valign=Gtk.Align.CENTER)
        self._busy = Gtk.Box(spacing=8, visible=False, valign=Gtk.Align.CENTER)
        self._busy.append(self._busy_spinner)
        self._busy.append(widgets.label(_("Loading…"), "dim-label", "caption"))
        bar.append(self._busy)
        self.append(widgets.scroller(bar))

        # Scope switcher above the state filters in the window sidebar.
        self.sidebar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin_bottom=12)
        # The sidebar sits in the window, outside this view: give it the actions too.
        self.sidebar.insert_action_group("services", actions)
        switcher = self._build_scope_switcher()
        switcher.set_margin_start(6)
        switcher.set_margin_end(6)
        switcher.set_margin_top(6)
        switcher.set_halign(Gtk.Align.FILL)
        switcher.set_hexpand(True)
        self.sidebar.append(switcher)
        self._kind_rows: dict[str, FilterRow] = {}
        self.filter_list = self._filter_list(self.KIND_FILTERS, self._kind_rows, self._on_filter_row_selected)
        self.filter_list.select_row(self._kind_rows["running"])
        self.sidebar.append(self.filter_list)
        self._type_rows: dict[str, FilterRow] = {}
        self.type_list = Gtk.ListBox(css_classes=["navigation-sidebar"])
        for value, text, icon, tip in self.TYPE_FILTERS:
            row = FilterRow(value, text, icon_name=icon, tooltip_text=tip)
            self._type_rows[value] = row
            self.type_list.append(row)
        self.type_list.set_header_func(_list_heading(_("Type")))
        self.type_list.select_row(self._type_rows["service"])
        self.type_list.connect("row-selected", self._on_type_row_selected)
        self.sidebar.append(self.type_list)
        self.filter_lists = (self.filter_list, self.type_list)

        self.stack = Gtk.Stack(vexpand=True, hhomogeneous=False, transition_type=Gtk.StackTransitionType.CROSSFADE)
        # Sorted by name. A list view
        # only makes rows for what is on screen; a row for each of hundreds of
        # services took seconds to build.
        self._store = Gio.ListStore(item_type=GObject.Object)
        order = Gtk.CustomSorter.new(lambda a, b, _d: _compare(_sort_key(a), _sort_key(b)))
        self.model = Gtk.SortListModel(model=self._store, sorter=order)
        # Not GTK's selection: single-click-activate selects on hover, and arrow keys
        # select without opening. The open service is highlighted by the rows themselves.
        self._selection = Gtk.NoSelection(model=self.model)
        self._selected_unit: Unit | None = None  # open in details; its row is highlighted
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
        self.simple_list.update_property([Gtk.AccessibleProperty.LABEL], [_("Units")])
        self.simple_list.connect("activate", self._on_row_activated)
        self._simple_scroll = Gtk.ScrolledWindow(child=self.simple_list, hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.stack.add_named(self._simple_scroll, "simple")

        self.empty_page = Adw.StatusPage(icon_name="systemdpilot-edit-find-symbolic")
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
        self._sync_open()

    def focus_list(self) -> None:
        """Put keyboard focus on the first service, so arrow keys move through the list."""
        if not self.simple_list.get_mapped():
            # The first list can arrive before the window shows, and showing it
            # focuses the first button: wait until after that.
            def mapped(*_):
                self.simple_list.disconnect(handler)
                GLib.idle_add(lambda: self.focus_list() and False)

            handler = self.simple_list.connect("map", mapped)
            return
        if self.model.get_n_items():
            # Before the rows are laid out, this only moves the list's cursor.
            self.simple_list.scroll_to(0, Gtk.ListScrollFlags.FOCUS, None)
            self.simple_list.grab_focus()

    def is_open(self, unit: Unit) -> bool:
        return self._selected_unit is not None and unit.key == self._selected_unit.key

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

    def set_show_inactive(self, show: bool) -> None:
        if show == self._show_inactive:
            return
        self._show_inactive = show
        self._refresh()

    @property
    def scope_filter(self) -> str:
        return self._scope.get_state().get_string()

    @property
    def kind_filter(self) -> str:
        return self._filter.get_state().get_string()

    @property
    def type_filter(self) -> str:
        return self._type.get_state().get_string()

    # -- internals --------------------------------------------------------

    def _build_scope_switcher(self) -> Gtk.Widget:
        """Linked All / System / User toggles; the scope action keeps one of them active."""
        box = Gtk.Box(css_classes=["linked"], valign=Gtk.Align.CENTER, homogeneous=True)
        for value, text, tip in self.SCOPE_FILTERS:
            button = Gtk.ToggleButton(
                label=text,
                tooltip_text=tip,
                hexpand=True,
                action_name="services.scope",
                action_target=GLib.Variant("s", value),
            )
            box.append(button)
        return box

    def _filter_list(self, filters, rows: dict, on_selected) -> Gtk.ListBox:
        listbox = Gtk.ListBox(css_classes=["navigation-sidebar"])
        for value, text, dot_kind, tip in (*filters, *self.PRESET_FILTERS, self.ANY_FILTER):
            row = FilterRow(value, text, dot_kind, tooltip_text=tip)
            rows[value] = row
            listbox.append(row)
        listbox.set_header_func(_list_heading(_("Status"), first=True))
        listbox.connect("row-selected", on_selected)
        return listbox

    def _on_filter_row_selected(self, _listbox, row):
        if row is not None and row.value != self.kind_filter:
            self._filter.change_state(GLib.Variant("s", row.value))

    def _on_type_row_selected(self, _listbox, row):
        if row is not None and row.value != self.type_filter:
            self._type.change_state(GLib.Variant("s", row.value))

    def _on_type(self, action, value):
        action.set_state(value)
        self.type_list.select_row(self._type_rows[value.get_string()])
        self._name_running_row()
        self._apply_filters()

    def _name_running_row(self) -> None:
        # Only services run: other active units are plugged, mounted, waiting, listening.
        if self.type_filter == "service":
            self._kind_rows["running"].set_text("running", "active (running)")
        else:
            self._kind_rows["running"].set_text("active", "ActiveState=active")

    def _on_scope(self, action, value):
        if value.get_string() == self.scope_filter:
            return
        action.set_state(value)
        self._apply_filters()

    def _on_filter(self, action, value):
        action.set_state(value)
        self.filter_list.select_row(self._kind_rows[value.get_string()])
        self._apply_filters()

    def _apply_filters(self) -> None:
        self._refresh()
        if self.model.get_n_items():
            self.simple_list.scroll_to(0, Gtk.ListScrollFlags.NONE, None)
        self.emit("filter-changed")

    def _matching(self) -> list[Unit]:
        q = self._query
        return [u for u in self._units if not q or q in u.name.lower() or q in u.description.lower()]

    def _visible(self) -> list[Unit]:
        scope, kind, unit_type = self.scope_filter, self.kind_filter, self.type_filter
        return [
            u for u in self._matching() if matches_filter(u, scope, kind, unit_type, show_inactive=self._show_inactive)
        ]

    def _refresh(self) -> None:
        matching = self._matching()
        scope, kind, unit_type = self.scope_filter, self.kind_filter, self.type_filter
        show_inactive = self._show_inactive
        # Each list counts what its rows would show with the other filters kept.
        for value, row in self._kind_rows.items():
            row.set_count(
                sum(1 for u in matching if matches_filter(u, scope, value, unit_type, show_inactive=show_inactive))
            )
        for value, row in self._type_rows.items():
            row.set_count(
                sum(1 for u in matching if matches_filter(u, scope, kind, value, show_inactive=show_inactive))
            )

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
            self.empty_page.set_title(_("No Units Found"))
            # Only “any” leaves stopped units out; the other statuses never or always include them.
            hint = self._empty_hint if self.kind_filter == "all" else ""
            self.empty_page.set_description(GLib.markup_escape_text(hint))
        self.stack.set_visible_child_name("empty")

    def _update_simple(self, visible: list[Unit]) -> None:
        """Change the simple list in place, so scrolling and focus are kept."""
        incoming = {u.key: u for u in visible}
        gone: list[int] = []
        for position in range(self._store.get_n_items()):
            item = self._store.get_item(position)
            unit = incoming.pop(item.unit.key, None)
            if unit is None:
                gone.append(position)
            elif unit != item.unit:
                item.unit = unit
                item.emit("changed")
        new = list(incoming.values())
        if len(new) > FILL_CHUNK:
            new = sorted(new, key=_unit_order)[:FILL_CHUNK]
            if not self._fill_source:
                self._fill_source = GLib.idle_add(self._fill_more)
        for start, count in reversed(_runs(gone)):
            self._store.splice(start, count, [])
        if new:
            self._store.splice(self._store.get_n_items(), 0, [ServiceItem(u) for u in new])
        if gone or new:
            # Rows whose place changed, e.g. the list's new last unit.
            for row in self._bound_rows():
                row.sync()
        self._sync_open()

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
        self._selected_unit = item.unit
        self.emit("unit-activated", item.unit)

    def _sync_open(self) -> None:
        """Highlight the open service's row, if it is shown."""
        unit = self._selected_unit
        if unit is not None:
            for position in range(self._store.get_n_items()):
                item = self._store.get_item(position)
                if item.unit.key == unit.key:
                    self._selected_unit = item.unit  # the list's current object
                    break
        for row in self._bound_rows():
            row.service.sync_classes()

    def _refresh_row_labels(self) -> None:
        for row in self._bound_rows():
            row.service.refresh_labels()


def _compare(a, b) -> int:
    return (a > b) - (a < b)


def _sort_key(item: ServiceItem) -> tuple:
    return _unit_order(item.unit)


def _unit_order(unit: Unit) -> tuple:
    return unit.name.lower(), unit.is_user


def _runs(positions: list[int]) -> list[tuple[int, int]]:
    """Ascending positions as (start, count) runs of neighbours."""
    runs: list[tuple[int, int]] = []
    for position in positions:
        if runs and runs[-1][0] + runs[-1][1] == position:
            runs[-1] = (runs[-1][0], runs[-1][1] + 1)
        else:
            runs.append((position, 1))
    return runs
