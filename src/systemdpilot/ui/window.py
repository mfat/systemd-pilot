"""Main window: machines in the sidebar, their services in the content pane."""

from __future__ import annotations

from gettext import gettext as _
from gettext import ngettext

from gi.repository import Adw, Gio, GLib, Gtk, Pango

from .. import RESOURCE_PATH
from ..core.errors import (
    AuthenticationFailed,
    ConnectionFailed,
    HostKeyMismatch,
    HostKeyUnknown,
)
from ..core.models import AuthMethod, Host, Scope, Unit, UnitAction
from ..core.session import LOCAL_ID, Sessions
from . import prompts
from .create_unit_dialog import CreateUnitDialog
from .host_dialog import HostDialog
from .operations import Operations, describe
from .settings import Settings
from .tasks import run_in_thread
from .unit_dialog import UnitDialog
from .unit_list import UnitList


class MachineRow(Gtk.ListBoxRow):
    def __init__(self, machine_id: str, title: str, subtitle: str, icon_name: str):
        super().__init__()
        self.machine_id = machine_id
        box = Gtk.Box(spacing=12, margin_top=6, margin_bottom=6, margin_start=6, margin_end=6)
        box.append(Gtk.Image(icon_name=icon_name))
        labels = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
        labels.append(Gtk.Label(label=title, xalign=0, ellipsize=Pango.EllipsizeMode.END))
        labels.append(
            Gtk.Label(label=subtitle, xalign=0, ellipsize=Pango.EllipsizeMode.END, css_classes=["dim-label", "caption"])
        )
        box.append(labels)
        self.status = Gtk.Image(
            icon_name="network-wired-symbolic", visible=False, tooltip_text=_("Connected"), css_classes=["success"]
        )
        box.append(self.status)
        self.set_child(box)
        self.update_property([Gtk.AccessibleProperty.LABEL], [title])

    def set_connected(self, connected: bool) -> None:
        self.status.set_visible(connected)


@Gtk.Template(resource_path=f"{RESOURCE_PATH}/ui/window.ui")
class Window(Adw.ApplicationWindow):
    __gtype_name__ = "SystemdPilotWindow"

    toast_overlay: Adw.ToastOverlay = Gtk.Template.Child()
    split_view: Adw.NavigationSplitView = Gtk.Template.Child()
    machine_list: Gtk.ListBox = Gtk.Template.Child()
    content_page: Adw.NavigationPage = Gtk.Template.Child()
    window_title: Adw.WindowTitle = Gtk.Template.Child()
    host_menu_button: Gtk.MenuButton = Gtk.Template.Child()
    search_bar: Gtk.SearchBar = Gtk.Template.Child()
    search_entry: Gtk.SearchEntry = Gtk.Template.Child()
    content_stack: Gtk.Stack = Gtk.Template.Child()
    spinner: Gtk.Spinner = Gtk.Template.Child()
    loading_label: Gtk.Label = Gtk.Template.Child()
    cancel_connect_button: Gtk.Button = Gtk.Template.Child()
    unit_list_bin: Adw.Bin = Gtk.Template.Child()
    empty_page: Adw.StatusPage = Gtk.Template.Child()
    disconnected_page: Adw.StatusPage = Gtk.Template.Child()
    error_page: Adw.StatusPage = Gtk.Template.Child()
    error_edit_button: Gtk.Button = Gtk.Template.Child()
    unit_menu: Gio.MenuModel = Gtk.Template.Child()

    def __init__(self, *, application: Adw.Application, sessions: Sessions, settings: Settings):
        super().__init__(application=application)
        self.sessions = sessions
        self.settings = settings
        self.machine_id = LOCAL_ID
        self.scope = Scope.SYSTEM
        self._generation = 0
        self._connecting: dict[str, bool] = {}  # host id -> cancelled
        self.operations = Operations(self, self.toast)

        self.set_default_size(settings.get_int("window-width"), settings.get_int("window-height"))
        if settings.get_boolean("window-maximized"):
            self.maximize()

        self.unit_list = UnitList(self.unit_menu)
        self.unit_list.connect("unit-activated", lambda _l, unit: self.show_unit(unit))
        self.unit_list.connect("selection-changed", lambda *_: self._update_actions())
        self.unit_list_bin.set_child(self.unit_list)
        self.search_bar.set_key_capture_widget(self)
        self.search_bar.connect("notify::search-mode-enabled", self._on_search_mode)

        self._setup_actions()
        self._rebuild_machine_list()
        self.machine_list.select_row(self.machine_list.get_row_at_index(0))

    # -- actions ----------------------------------------------------------

    def _setup_actions(self):
        def add(name, callback, group=self):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda *_: callback())
            group.add_action(action)
            return action

        add("refresh", self.reload)
        add("search", lambda: self.search_bar.set_search_mode(True))
        add("add-host", self.add_host)
        add("edit-host", self.edit_host)
        add("remove-host", self.remove_host)
        add("connect", self.connect_current)
        add("cancel-connect", self.cancel_connect)
        add("disconnect", self.disconnect_current)
        add("daemon-reload", self.daemon_reload)
        add("create-unit", self.create_unit)

        scope = Gio.SimpleAction.new_stateful("scope", GLib.VariantType.new("s"), GLib.Variant("s", self.scope.value))
        scope.connect("change-state", self._on_scope_changed)
        self.add_action(scope)

        inactive = Gio.SimpleAction.new_stateful(
            "show-inactive", None, GLib.Variant("b", self.settings.get_boolean("show-inactive"))
        )
        inactive.connect("change-state", self._on_show_inactive_changed)
        self.add_action(inactive)

        self.unit_actions = Gio.SimpleActionGroup()
        for action in UnitAction:
            if action is not UnitAction.RELOAD:
                add(action.value, lambda a=action: self.control_selected(a), self.unit_actions)
        add("details", lambda: self.show_unit(self.unit_list.selected_unit), self.unit_actions)
        self.insert_action_group("unit", self.unit_actions)

    def _enable(self, name, enabled, group=None):
        (group or self).lookup_action(name).set_enabled(enabled)

    def _update_actions(self):
        remote = self.machine_id != LOCAL_ID
        connected = self.sessions.is_connected(self.machine_id)
        connecting = self.machine_id in self._connecting
        self._enable("connect", remote and not connected and not connecting)
        self._enable("disconnect", remote and connected)
        self._enable("edit-host", remote)
        self._enable("remove-host", remote)
        for name in ("refresh", "daemon-reload", "create-unit", "scope", "show-inactive"):
            self._enable(name, connected)
        self._enable("refresh", connected or (remote and not connecting))
        has_unit = connected and self.unit_list.selected_unit is not None
        for name in self.unit_actions.list_actions():
            self._enable(name, has_unit, self.unit_actions)
        self.host_menu_button.set_visible(remote)

    def _on_scope_changed(self, action, value):
        action.set_state(value)
        self.scope = Scope(value.get_string())
        self.reload(show_spinner=True)

    def _on_show_inactive_changed(self, action, value):
        action.set_state(value)
        self.settings.set_boolean("show-inactive", value.get_boolean())
        self.reload()

    @property
    def show_inactive(self) -> bool:
        return self.lookup_action("show-inactive").get_state().get_boolean()

    # -- machines ---------------------------------------------------------

    def _rebuild_machine_list(self):
        self.machine_list.remove_all()
        local = MachineRow(LOCAL_ID, _("This Computer"), GLib.get_host_name(), "computer-symbolic")
        local.set_connected(False)
        self.machine_list.append(local)
        for host in self.sessions.hosts.hosts():
            row = MachineRow(host.id, host.name, f"{host.username}@{host.hostname}", "network-server-symbolic")
            row.set_connected(self.sessions.is_connected(host.id))
            self.machine_list.append(row)

    def _row_for(self, machine_id):
        row = self.machine_list.get_first_child()
        while row:
            if isinstance(row, MachineRow) and row.machine_id == machine_id:
                return row
            row = row.get_next_sibling()
        return None

    def _refresh_row_status(self, machine_id):
        row = self._row_for(machine_id)
        if row and machine_id != LOCAL_ID:
            row.set_connected(self.sessions.is_connected(machine_id))

    def _current_host(self) -> Host | None:
        return self.sessions.hosts.get(self.machine_id)

    @Gtk.Template.Callback()
    def on_machine_selected(self, _listbox, row):
        if row is None or row.machine_id == self.machine_id and self.unit_list.store.get_n_items():
            return
        self.machine_id = row.machine_id
        self.unit_list.clear()
        host = self._current_host()
        self.content_page.set_title(host.name if host else _("This Computer"))
        self.window_title.set_title(self.content_page.get_title())
        self.window_title.set_subtitle("")
        if self.sessions.is_connected(self.machine_id):
            self.reload(show_spinner=True)
        elif self.machine_id in self._connecting:
            self._show_loading(_("Connecting to {host}…").format(host=host.name), cancellable=True)
        else:
            self._show_disconnected()
        self._update_actions()

    @Gtk.Template.Callback()
    def on_machine_activated(self, _listbox, row):
        if row.machine_id != LOCAL_ID and not self.sessions.is_connected(row.machine_id):
            self.connect_current()
        self.split_view.set_show_content(True)

    # -- connecting -------------------------------------------------------

    def connect_current(self, secret: str | None = None):
        host = self._current_host()
        if not host or self.sessions.is_connected(host.id) or host.id in self._connecting:
            return
        if host.auth is AuthMethod.PASSWORD and secret is None and not self.sessions.hosts.secret(host):
            self._ask_login_password(host)
            return

        self._connecting[host.id] = False
        self._show_loading(_("Connecting to {host}…").format(host=host.name), cancellable=True)
        self._update_actions()
        run_in_thread(
            self.sessions.connect,
            host,
            secret,
            on_done=lambda _m: self._on_connected(host),
            on_error=lambda e: self._on_connect_failed(host, e),
        )

    def cancel_connect(self):
        host = self._current_host()
        if host and host.id in self._connecting:
            self._connecting[host.id] = True
            self._show_disconnected()
            self._update_actions()

    def _on_connected(self, host):
        cancelled = self._connecting.pop(host.id, False)
        if cancelled:
            self.sessions.disconnect(host.id)
            return
        self._refresh_row_status(host.id)
        if self.machine_id == host.id:
            self._update_actions()
            self.reload(show_spinner=True)
        else:
            self.toast(_("Connected to {host}").format(host=host.name))

    def _on_connect_failed(self, host, error):
        cancelled = self._connecting.pop(host.id, False)
        visible = self.machine_id == host.id
        if visible:
            self._update_actions()
        if cancelled:
            return

        if isinstance(error, HostKeyUnknown):

            def on_trust(trusted):
                if trusted:
                    self.sessions.known_hosts.trust(error.hostname, error.port, error.key)
                    self.connect_current()
                elif visible:
                    self._show_disconnected()

            prompts.ask_trust_host_key(self, host.hostname, error.key_type, error.fingerprint, on_trust)
            if visible:
                self._show_disconnected()
            return

        if isinstance(error, AuthenticationFailed) and host.auth is AuthMethod.PASSWORD:
            self._ask_login_password(host, error=_("The password was not accepted. Try again."))
            if visible:
                self._show_disconnected()
            return

        if isinstance(error, HostKeyMismatch):
            title = _("Host Key Changed")
        elif isinstance(error, AuthenticationFailed):
            title = _("Authentication Failed")
        else:
            title = _("Could Not Connect")
        if visible:
            self._show_error(title, describe(error), offer_edit=True)
        else:
            self.toast(_("Could not connect to {host}").format(host=host.name))

    def _ask_login_password(self, host, error=""):
        def on_password(password, remember):
            if password is None:
                return
            if remember:
                self.sessions.hosts.save(host, password)
            if self.machine_id == host.id:
                self.connect_current(secret=password)

        prompts.ask_password(
            self,
            _("Log In to {host}").format(host=host.name),
            _("Enter the password for {user}@{hostname}.").format(user=host.username, hostname=host.hostname),
            on_password,
            error=error,
            offer_remember=True,
        )

    def disconnect_current(self):
        self.sessions.disconnect(self.machine_id)
        self._refresh_row_status(self.machine_id)
        self.unit_list.clear()
        self._show_disconnected()
        self._update_actions()

    # -- host management --------------------------------------------------

    def add_host(self):
        dialog = HostDialog(self.sessions.hosts)
        dialog.connect("saved", self._on_host_saved)
        dialog.present(self)

    def edit_host(self):
        host = self._current_host()
        if not host:
            return
        dialog = HostDialog(self.sessions.hosts, host)
        dialog.connect("saved", self._on_host_saved)
        dialog.connect("remove-requested", lambda *_: self.remove_host())
        dialog.present(self)

    def _on_host_saved(self, _dialog, host_id):
        # Settings may have changed; reconnect with the new ones next time.
        self.sessions.disconnect(host_id)
        self._rebuild_machine_list()
        self.machine_id = None
        self.machine_list.select_row(self._row_for(host_id))

    def remove_host(self):
        host = self._current_host()
        if not host:
            return
        prompts.confirm(
            self,
            _("Remove {host}?").format(host=host.name),
            _("The host and its saved password will be removed."),
            _("_Remove"),
            lambda: self._remove_host(host.id),
            destructive=True,
        )

    def _remove_host(self, host_id):
        self.sessions.disconnect(host_id)
        self.sessions.hosts.remove(host_id)
        self._rebuild_machine_list()
        self.machine_id = None
        self.machine_list.select_row(self.machine_list.get_row_at_index(0))

    # -- units ------------------------------------------------------------

    def reload(self, show_spinner: bool = False):
        manager = self.sessions.get(self.machine_id)
        if manager is None:
            if self.machine_id != LOCAL_ID and self.machine_id not in self._connecting:
                self.connect_current()
            return
        self._generation += 1
        generation = self._generation
        machine_id, scope, include_inactive = self.machine_id, self.scope, self.show_inactive
        if show_spinner or not self.unit_list.store.get_n_items():
            self._show_loading(_("Loading services…"))

        # Loaded units come back almost instantly; startup states and units
        # that are not loaded take systemd much longer, so they follow later.
        def done(units):
            if generation != self._generation:
                return
            self._on_units_loaded(units)
            run_in_thread(
                manager.complete_units, units, scope, include_inactive, on_done=completed, on_error=lambda _e: None
            )

        def completed(units):
            if generation == self._generation:
                self._on_units_loaded(units)

        def failed(error):
            if generation != self._generation:
                return
            if isinstance(error, ConnectionFailed) and machine_id != LOCAL_ID:
                self.sessions.disconnect(machine_id)
                self._refresh_row_status(machine_id)
                self._update_actions()
                self._show_error(_("Connection Lost"), describe(error))
            else:
                self._show_error(_("Could Not Load Services"), describe(error))

        run_in_thread(manager.list_units, scope, include_inactive, on_done=done, on_error=failed)

    def _on_units_loaded(self, units: list[Unit]):
        self.unit_list.set_units(units)
        scope = _("User services") if self.scope is Scope.USER else _("System services")
        count = ngettext("{n} service", "{n} services", len(units)).format(n=len(units))
        self.window_title.set_subtitle(f"{scope} · {count}")
        self._update_list_page()
        self._update_actions()

    def _update_list_page(self):
        if self.unit_list.visible_count:
            self.content_stack.set_visible_child_name("units")
            self.spinner.stop()
            return
        query = self.search_entry.get_text().strip()
        if query:
            self.empty_page.set_title(_("No Results Found"))
            message = _("Nothing matches “{query}”.").format(query=query)
            self.empty_page.set_description(GLib.markup_escape_text(message))
        else:
            self.empty_page.set_title(_("No Services Found"))
            self.empty_page.set_description(
                _("Use “Show Inactive Services” in the main menu to include stopped services.")
                if not self.show_inactive
                else ""
            )
        self.content_stack.set_visible_child_name("empty")
        self.spinner.stop()

    @Gtk.Template.Callback()
    def on_search_changed(self, entry):
        self.unit_list.set_query(entry.get_text())
        if self.content_stack.get_visible_child_name() in ("units", "empty"):
            self._update_list_page()

    def _on_search_mode(self, bar, _pspec):
        if not bar.get_search_mode():
            self.search_entry.set_text("")

    def control_selected(self, action: UnitAction):
        unit = self.unit_list.selected_unit
        manager = self.sessions.get(self.machine_id)
        if not unit or not manager:
            return
        scope = self.scope
        self.operations.run(
            manager,
            lambda: manager.control(unit.name, action, scope),
            on_success=lambda _r: (self.toast(self._action_message(unit, action)), self.reload()),
            error_heading=_("Could Not {action} {unit}").format(action=action.value.title(), unit=unit.short_name),
        )

    @staticmethod
    def _action_message(unit: Unit, action: UnitAction) -> str:
        messages = {
            UnitAction.START: _("Started {unit}"),
            UnitAction.STOP: _("Stopped {unit}"),
            UnitAction.RESTART: _("Restarted {unit}"),
            UnitAction.RELOAD: _("Reloaded {unit}"),
            UnitAction.ENABLE: _("Enabled {unit}"),
            UnitAction.DISABLE: _("Disabled {unit}"),
        }
        return messages[action].format(unit=unit.short_name)

    def show_unit(self, unit: Unit | None):
        manager = self.sessions.get(self.machine_id)
        if not unit or not manager:
            return
        dialog = UnitDialog(
            manager, unit, self.scope, self.operations, on_changed=self.reload, action_message=self._action_message
        )
        dialog.present(self)

    def daemon_reload(self):
        manager = self.sessions.get(self.machine_id)
        if not manager:
            return
        scope = self.scope
        self.operations.run(
            manager,
            lambda: manager.daemon_reload(scope),
            on_success=lambda _r: (self.toast(_("systemd configuration reloaded")), self.reload()),
            error_heading=_("Could Not Reload Configuration"),
        )

    def create_unit(self):
        manager = self.sessions.get(self.machine_id)
        if not manager:
            return
        host = self._current_host()
        dialog = CreateUnitDialog(
            manager,
            self.scope,
            host.name if host else _("This Computer"),
            self.operations,
            on_created=lambda name: (self.toast(_("Created {unit}").format(unit=name)), self.reload()),
        )
        dialog.present(self)

    # -- pages ------------------------------------------------------------

    def _show_loading(self, text: str, cancellable: bool = False):
        self.loading_label.set_label(text)
        self.cancel_connect_button.set_visible(cancellable)
        self.spinner.start()
        self.content_stack.set_visible_child_name("loading")

    def _show_disconnected(self):
        host = self._current_host()
        self.spinner.stop()
        if host:
            self.disconnected_page.set_description(
                GLib.markup_escape_text(f"{host.username}@{host.hostname}:{host.port}")
            )
        self.content_stack.set_visible_child_name("disconnected")

    def _show_error(self, title: str, message: str, offer_edit: bool = False):
        self.spinner.stop()
        self.error_page.set_title(title)
        self.error_page.set_description(GLib.markup_escape_text(message))
        self.error_edit_button.set_visible(offer_edit and self.machine_id != LOCAL_ID)
        self.content_stack.set_visible_child_name("error")

    def toast(self, message: str):
        self.toast_overlay.add_toast(Adw.Toast(title=message, use_markup=False, timeout=3))

    # -- lifecycle --------------------------------------------------------

    def do_close_request(self):
        if not self.is_maximized():
            width, height = self.get_default_size()
            self.settings.set_int("window-width", width)
            self.settings.set_int("window-height", height)
        self.settings.set_boolean("window-maximized", self.is_maximized())
        return False
