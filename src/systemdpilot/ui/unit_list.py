"""Sortable, searchable list of units."""

from __future__ import annotations

import dataclasses
from gettext import gettext as _

from gi.repository import Gdk, Gio, GObject, Graphene, Gtk, Pango

from ..core.models import Unit


class UnitItem(GObject.Object):
    __gtype_name__ = "SystemdPilotUnitItem"

    name = GObject.Property(type=str, default="")
    description = GObject.Property(type=str, default="")
    state = GObject.Property(type=str, default="")
    startup = GObject.Property(type=str, default="")

    def __init__(self, unit: Unit):
        super().__init__()
        self.update(unit)

    def update(self, unit: Unit) -> None:
        # While startup states are still loading, keep showing the previous one.
        if unit.file_state is None and getattr(self, "unit", None) is not None:
            unit = dataclasses.replace(unit, file_state=self.unit.file_state)
        self.unit = unit
        self.name = unit.short_name
        self.description = unit.description
        self.state = unit.state_label
        self.startup = unit.file_state or ""


def state_css_class(unit: Unit) -> str:
    if unit.is_failed:
        return "error"
    if unit.active_state in ("activating", "deactivating", "reloading"):
        return "warning"
    if unit.is_active:
        return "success"
    return "dim-label"


class UnitList(Gtk.ScrolledWindow):
    """Shows units in a :class:`Gtk.ColumnView`.

    Emits ``unit-activated`` when a row is activated. Right-clicking (or
    long-pressing) a row selects it and shows ``context_menu``.
    """

    __gtype_name__ = "SystemdPilotUnitList"
    __gsignals__ = {
        "unit-activated": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        "selection-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self, context_menu: Gio.MenuModel):
        super().__init__(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        self._query = ""
        self._handlers: dict[Gtk.ListItem, tuple[UnitItem, int]] = {}

        self.store = Gio.ListStore(item_type=UnitItem)
        self._filter = Gtk.CustomFilter.new(self._matches)
        filtered = Gtk.FilterListModel(model=self.store, filter=self._filter)

        self.view = Gtk.ColumnView(
            show_row_separators=False,
            reorderable=False,
            single_click_activate=False,
            css_classes=["data-table"],
        )
        self.view.update_property([Gtk.AccessibleProperty.LABEL], [_("Services")])
        self._sorted = Gtk.SortListModel(model=filtered, sorter=self.view.get_sorter())
        self.selection = Gtk.SingleSelection(model=self._sorted, autoselect=False, can_unselect=True)
        self.selection.connect("selection-changed", lambda *_: self.emit("selection-changed"))
        self.view.set_model(self.selection)
        self.view.connect("activate", self._on_activate)

        name_col = self._add_column(_("Service"), "name", self._setup_name, self._render_name, expand=True)
        self._add_column(_("State"), "state", self._setup_label, self._render_state)
        self._add_column(_("Startup"), "startup", self._setup_label, self._render_startup)
        self.view.sort_by_column(name_col, Gtk.SortType.ASCENDING)

        self._menu = Gtk.PopoverMenu.new_from_model(context_menu)
        self._menu.set_parent(self.view)
        self._menu.set_has_arrow(False)
        self._menu.set_halign(Gtk.Align.START)
        self.connect("destroy", lambda *_: self._menu.unparent())

        # Keyboard access to the context menu: Menu key or Shift+F10.
        trigger = Gtk.ShortcutTrigger.parse_string("Menu|<Shift>F10")
        action = Gtk.CallbackAction.new(lambda *_: self._popup_for_focus())
        shortcuts = Gtk.ShortcutController(scope=Gtk.ShortcutScope.LOCAL)
        shortcuts.add_shortcut(Gtk.Shortcut(trigger=trigger, action=action))
        self.view.add_controller(shortcuts)

        self.set_child(self.view)

    # -- public API -------------------------------------------------------

    def set_units(self, units: list[Unit]) -> None:
        """Update the list in place, so scrolling and selection are kept."""
        incoming = {u.name: u for u in units}
        for i in reversed(range(self.store.get_n_items())):
            item = self.store.get_item(i)
            unit = incoming.pop(item.unit.name, None)
            if unit is None:
                self.store.remove(i)
            elif unit != item.unit:
                item.update(unit)
        self.store.splice(self.store.get_n_items(), 0, [UnitItem(u) for u in incoming.values()])

    def clear(self) -> None:
        self.store.remove_all()

    @property
    def visible_count(self) -> int:
        return self._sorted.get_n_items()

    @property
    def selected_unit(self) -> Unit | None:
        item = self.selection.get_selected_item()
        return item.unit if item else None

    def select_name(self, name: str) -> None:
        for i in range(self._sorted.get_n_items()):
            if self._sorted.get_item(i).unit.name == name:
                self.selection.set_selected(i)
                return
        self.selection.set_selected(Gtk.INVALID_LIST_POSITION)

    def set_query(self, query: str) -> None:
        query = query.strip().lower()
        if query == self._query:
            return
        change = (
            Gtk.FilterChange.MORE_STRICT
            if query.startswith(self._query)
            else Gtk.FilterChange.LESS_STRICT
            if self._query.startswith(query)
            else Gtk.FilterChange.DIFFERENT
        )
        self._query = query
        self._filter.changed(change)

    # -- internals --------------------------------------------------------

    def _matches(self, item: UnitItem) -> bool:
        if not self._query:
            return True
        return self._query in item.unit.name.lower() or self._query in item.unit.description.lower()

    def _add_column(self, title, prop, setup, render, expand=False):
        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", setup)
        factory.connect("bind", self._bind, render)
        factory.connect("unbind", self._unbind)
        column = Gtk.ColumnViewColumn(title=title, factory=factory, expand=expand, resizable=True)
        expression = Gtk.PropertyExpression.new(UnitItem.__gtype__, None, prop)
        column.set_sorter(Gtk.StringSorter(expression=expression, ignore_case=True))
        self.view.append_column(column)
        return column

    def _add_context_gestures(self, widget: Gtk.Widget, list_item: Gtk.ListItem) -> None:
        click = Gtk.GestureClick(button=3)
        click.connect("pressed", lambda _g, _n, x, y: self._popup(widget, list_item, x, y))
        widget.add_controller(click)
        press = Gtk.GestureLongPress(touch_only=True)
        press.connect("pressed", lambda _g, x, y: self._popup(widget, list_item, x, y))
        widget.add_controller(press)

    def _popup(self, widget, list_item, x, y):
        self.selection.set_selected(list_item.get_position())
        ok, point = widget.compute_point(self.view, Graphene.Point().init(x, y))
        if not ok:
            return
        rect = Gdk.Rectangle()
        rect.x, rect.y, rect.width, rect.height = int(point.x), int(point.y), 1, 1
        self._menu.set_pointing_to(rect)
        self._menu.popup()

    def _bind(self, _factory, list_item, render):
        item = list_item.get_item()
        render(list_item)
        # Re-render when the item is updated in place.
        self._handlers[list_item] = (item, item.connect("notify", lambda *_: render(list_item)))

    def _unbind(self, _factory, list_item):
        item, handler = self._handlers.pop(list_item, (None, None))
        if item is not None:
            item.disconnect(handler)

    def _popup_for_focus(self) -> bool:
        """Show the context menu for the selected row, next to the focused cell."""
        if self.selection.get_selected_item() is None:
            return False
        focus = self.get_root().get_focus() if self.get_root() else None
        rect = Gdk.Rectangle()
        ok, bounds = focus.compute_bounds(self.view) if focus and focus.is_ancestor(self.view) else (False, None)
        if ok:
            rect.x, rect.y = int(bounds.get_x() + 24), int(bounds.get_y() + bounds.get_height() / 2)
        else:
            rect.x, rect.y = 24, 24
        rect.width = rect.height = 1
        self._menu.set_pointing_to(rect)
        self._menu.popup()
        return True

    def _setup_name(self, _factory, list_item):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, margin_top=6, margin_bottom=6)
        title = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END, css_classes=["heading"])
        subtitle = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END, css_classes=["dim-label", "caption"])
        box.append(title)
        box.append(subtitle)
        list_item.set_child(box)
        self._add_context_gestures(box, list_item)

    def _render_name(self, list_item):
        item = list_item.get_item()
        box = list_item.get_child()
        title, subtitle = box.get_first_child(), box.get_last_child()
        title.set_label(item.name)
        title.set_tooltip_text(item.unit.name)
        subtitle.set_label(item.description)
        subtitle.set_visible(bool(item.description))

    def _setup_label(self, _factory, list_item):
        label = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
        list_item.set_child(label)
        self._add_context_gestures(label, list_item)

    def _render_state(self, list_item):
        item = list_item.get_item()
        label = list_item.get_child()
        label.set_label(item.state)
        label.set_css_classes([state_css_class(item.unit)])

    def _render_startup(self, list_item):
        item = list_item.get_item()
        label = list_item.get_child()
        label.set_label(item.startup or "—")
        label.set_css_classes(["dim-label"] if item.startup not in ("enabled", "enabled-runtime") else [])

    def _on_activate(self, _view, position):
        item = self._sorted.get_item(position)
        if item:
            self.emit("unit-activated", item.unit)
