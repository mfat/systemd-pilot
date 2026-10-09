"""Details, logs and controls for one unit."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from gettext import gettext as _

from gi.repository import Adw, GLib, Gtk

from ..core.manager import SystemdManager
from ..core.models import LogResult, Scope, Unit, UnitAction
from . import text
from .operations import Operations, describe
from .resources import template
from .tasks import run_in_thread
from .unit_list import state_css_class

_BUTTONS = (
    (UnitAction.START, _("_Start"), "media-playback-start-symbolic"),
    (UnitAction.STOP, _("S_top"), "media-playback-stop-symbolic"),
    (UnitAction.RESTART, _("_Restart"), "view-refresh-symbolic"),
    (UnitAction.ENABLE, _("_Enable"), "emblem-ok-symbolic"),
    (UnitAction.DISABLE, _("_Disable"), "window-close-symbolic"),
)


@dataclass
class _Details:
    unit: Unit
    status: str
    properties: dict[str, str]
    unit_file: str
    logs: LogResult


@template("unit-dialog.ui")
class UnitDialog(Adw.Dialog):
    __gtype_name__ = "SystemdPilotUnitDialog"

    toast_overlay: Adw.ToastOverlay = Gtk.Template.Child()
    refresh_button: Gtk.Button = Gtk.Template.Child()
    name_label: Gtk.Label = Gtk.Template.Child()
    description_label: Gtk.Label = Gtk.Template.Child()
    active_badge: Gtk.Label = Gtk.Template.Child()
    startup_badge: Gtk.Label = Gtk.Template.Child()
    action_box: Gtk.FlowBox = Gtk.Template.Child()
    status_view: Gtk.TextView = Gtk.Template.Child()
    logs_banner: Adw.Banner = Gtk.Template.Child()
    logs_view: Gtk.TextView = Gtk.Template.Child()
    logs_scroll: Gtk.ScrolledWindow = Gtk.Template.Child()
    file_view: Gtk.TextView = Gtk.Template.Child()
    props_view: Gtk.TextView = Gtk.Template.Child()
    props_search: Gtk.SearchEntry = Gtk.Template.Child()

    def __init__(
        self,
        manager: SystemdManager,
        unit: Unit,
        scope: Scope,
        operations: Operations,
        *,
        on_changed: Callable[[], None],
        action_message: Callable[[Unit, UnitAction], str],
    ):
        super().__init__()
        self.manager = manager
        self.unit = unit
        self.scope = scope
        self.operations = operations
        self._on_changed = on_changed
        self._action_message = action_message
        self._properties: dict[str, str] = {}
        self._buttons: dict[UnitAction, Gtk.Button] = {}
        self._closed = False
        self.connect("closed", self._on_closed)

        self.set_title(unit.short_name)
        self.name_label.set_label(unit.name)
        self.description_label.set_label(unit.description)
        self.description_label.set_visible(bool(unit.description))

        for action, label, icon in _BUTTONS:
            content = Adw.ButtonContent(label=label, icon_name=icon, use_underline=True)
            button = Gtk.Button(child=content)
            if action is UnitAction.STOP:
                button.add_css_class("destructive-action")
            button.connect("clicked", lambda _b, a=action: self._run_action(a))
            self.action_box.append(button)
            self._buttons[action] = button

        self._show_unit(unit)
        self.load()

    def _on_closed(self, *_args):
        self._closed = True

    def _show_unit(self, unit: Unit):
        self.unit = unit
        self.active_badge.set_label(unit.state_label)
        self.active_badge.set_css_classes(["badge", state_css_class(unit)])
        file_state = unit.file_state or ""
        enabled = file_state.startswith("enabled")
        can_toggle = file_state not in ("", "static", "masked", "generated", "transient", "indirect", "alias")
        self.startup_badge.set_label(file_state)
        self.startup_badge.set_visible(bool(file_state))
        self.startup_badge.set_css_classes(["badge", "accent" if enabled else "dim-label"])

        self._buttons[UnitAction.START].set_sensitive(not unit.is_active)
        self._buttons[UnitAction.STOP].set_sensitive(unit.is_active)
        self._buttons[UnitAction.ENABLE].set_sensitive(can_toggle and not enabled)
        self._buttons[UnitAction.DISABLE].set_sensitive(can_toggle and enabled)

    def _set_busy(self, busy: bool):
        self.refresh_button.set_sensitive(not busy)
        self.action_box.set_sensitive(not busy)

    # -- loading ----------------------------------------------------------

    def load(self):
        self._set_busy(True)
        manager, name, scope = self.manager, self.unit.name, self.scope

        def fetch() -> _Details:
            props = manager.properties(name, scope)
            unit = Unit(
                name=name,
                description=props.get("Description", self.unit.description),
                load_state=props.get("LoadState", ""),
                active_state=props.get("ActiveState", ""),
                sub_state=props.get("SubState", ""),
                file_state=props.get("UnitFileState", ""),
            )
            return _Details(
                unit=unit,
                status=manager.status_text(name, scope),
                properties=props,
                unit_file=manager.unit_file(name, scope),
                logs=manager.logs(name, scope),
            )

        run_in_thread(fetch, on_done=self._on_loaded, on_error=self._on_load_failed)

    def _on_loaded(self, details: _Details):
        if self._closed:
            return
        self._set_busy(False)
        self._show_unit(details.unit)
        self.status_view.get_buffer().set_text(details.status)
        self.file_view.get_buffer().set_text(details.unit_file or _("No unit file found."))
        self._properties = details.properties
        text.set_properties(self.props_view.get_buffer(), self._properties, self.props_search.get_text())
        text.set_logs(self.logs_view.get_buffer(), details.logs.entries)
        if not details.logs.entries:
            self.logs_view.get_buffer().set_text(_("No log entries."))
        self.logs_banner.set_title(GLib.markup_escape_text(details.logs.warning))
        self.logs_banner.set_revealed(bool(details.logs.warning))
        GLib.idle_add(self._scroll_logs_to_end)

    def _scroll_logs_to_end(self):
        adj = self.logs_scroll.get_vadjustment()
        adj.set_value(adj.get_upper() - adj.get_page_size())
        return GLib.SOURCE_REMOVE

    def _on_load_failed(self, error):
        if self._closed:
            return
        self._set_busy(False)
        message = describe(error)
        self.status_view.get_buffer().set_text(message)
        self.toast_overlay.add_toast(Adw.Toast(title=_("Could not load details"), timeout=3))

    @Gtk.Template.Callback()
    def on_refresh_clicked(self, _button):
        self.load()

    @Gtk.Template.Callback()
    def on_props_search_changed(self, entry):
        text.set_properties(self.props_view.get_buffer(), self._properties, entry.get_text())

    # -- actions ----------------------------------------------------------

    def _run_action(self, action: UnitAction):
        self._set_busy(True)
        manager, name, scope = self.manager, self.unit.name, self.scope

        def success(_result):
            if not self._closed:
                self.toast_overlay.add_toast(
                    Adw.Toast(title=self._action_message(self.unit, action), use_markup=False, timeout=3)
                )
            self._on_changed()

        def finish():
            if not self._closed:
                self._set_busy(False)
                self.load()

        self.operations.run(
            manager,
            lambda: manager.control(name, action, scope),
            on_success=success,
            on_finish=finish,
            error_heading=_("Action Failed"),
        )
