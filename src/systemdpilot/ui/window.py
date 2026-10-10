"""Main window: machines in the sidebar, their services or journal in the content pane."""

from __future__ import annotations

import dataclasses
from gettext import gettext as _
from gettext import ngettext

from gi.repository import Adw, Gio, GLib, Gtk, Pango

from ..core.errors import (
    AuthenticationFailed,
    ConnectionCancelled,
    ConnectionFailed,
    HostKeyMismatch,
    HostKeyUnknown,
    PilotError,
)
from ..core.manager import SystemdManager
from ..core.models import AuthMethod, Host, Scope, Unit, UnitAction
from ..core.session import LOCAL_ID, Sessions
from ..core.ssh import SSHRunner
from . import prompts
from .create_unit_dialog import CreateUnitDialog
from .host_dialog import HostDialog
from .journal_view import JournalView
from .operations import Operations, describe
from .resources import template
from .services_view import ServicesView
from .settings import Settings
from .tasks import run_in_thread
from .unit_dialog import UnitDialog
from .widgets import dot, set_count_badge


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
        self.status = dot("running", small=True)
        self.status.set_tooltip_text(_("Connected"))
        self.status.set_visible(False)
        box.append(self.status)
        self.set_child(box)
        self.update_property([Gtk.AccessibleProperty.LABEL], [title])

    def set_connected(self, connected: bool) -> None:
        self.status.set_visible(connected)


@template("window.ui")
class Window(Adw.ApplicationWindow):
    __gtype_name__ = "SystemdPilotWindow"

    toast_overlay: Adw.ToastOverlay = Gtk.Template.Child()
    split_view: Adw.OverlaySplitView = Gtk.Template.Child()
    machine_list: Gtk.ListBox = Gtk.Template.Child()
    window_title: Adw.WindowTitle = Gtk.Template.Child()
    failed_badge: Gtk.Label = Gtk.Template.Child()
    issues_badge: Gtk.Label = Gtk.Template.Child()
    # The same switch at the bottom of narrow windows.
    failed_badge_bottom: Gtk.Label = Gtk.Template.Child()
    issues_badge_bottom: Gtk.Label = Gtk.Template.Child()
    host_menu_button: Gtk.MenuButton = Gtk.Template.Child()
    search_bar: Gtk.SearchBar = Gtk.Template.Child()
    search_entry: Gtk.SearchEntry = Gtk.Template.Child()
    content_stack: Gtk.Stack = Gtk.Template.Child()
    spinner: Gtk.Spinner = Gtk.Template.Child()
    loading_label: Gtk.Label = Gtk.Template.Child()
    cancel_connect_button: Gtk.Button = Gtk.Template.Child()
    view_stack: Gtk.Stack = Gtk.Template.Child()
    services_bin: Adw.Bin = Gtk.Template.Child()
    journal_bin: Adw.Bin = Gtk.Template.Child()
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
        self._runtime_loaded = False
        self._journal_badge_source = 0
        self._connecting: dict[str, SSHRunner] = {}  # host id -> connection attempt
        self.operations = Operations(self, self.toast)

        self.set_default_size(settings.get_int("window-width"), settings.get_int("window-height"))
        if settings.get_boolean("window-maximized"):
            self.maximize()

        self.services = ServicesView(self.unit_menu)
        self.services.connect("unit-activated", lambda _v, unit: self.show_unit(unit))
        self.services.connect("unit-action", lambda _v, unit, action: self.control_unit(unit, UnitAction(action)))
        self.services.connect("filter-changed", lambda *_: self._update_header())
        self.services_bin.set_child(self.services)
        # The advanced table; its selection drives the "unit" actions and context menu.
        self.unit_list = self.services.unit_list
        self.unit_list.connect("selection-changed", lambda *_: self._update_actions())
        self.journal = JournalView(self.operations)
        self.journal.connect("open-unit", lambda _v, name: self._open_unit_by_name(name))
        self.journal.connect("changed", lambda *_: self._update_header())
        self.journal_bin.set_child(self.journal)
        self.search_bar.set_key_capture_widget(self)
        self.search_bar.connect("notify::search-mode-enabled", self._on_search_mode)

        self._setup_actions()
        self.services.set_scope(self.scope)
        self.services.set_empty_hint(self._empty_hint())
        self._apply_mode()
        self._rebuild_machine_list()
        self.machine_list.select_row(self.machine_list.get_row_at_index(0))

    # -- actions ----------------------------------------------------------

    def _setup_actions(self):
        def add(name, callback, group=self):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda *_: callback())
            group.add_action(action)
            return action

        add("refresh", lambda: self.reload(refresh_files=True))
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

        view = Gio.SimpleAction.new_stateful("view", GLib.VariantType.new("s"), GLib.Variant("s", "services"))
        view.connect("change-state", self._on_view_changed)
        self.add_action(view)

        mode = self.settings.get_string("view-mode")
        mode = Gio.SimpleAction.new_stateful(
            "mode", GLib.VariantType.new("s"), GLib.Variant("s", mode if mode == "advanced" else "simple")
        )
        mode.connect("change-state", self._on_mode_changed)
        self.add_action(mode)

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
        for name in ("refresh", "daemon-reload", "create-unit", "scope", "show-inactive", "view"):
            self._enable(name, connected)
        self._enable("refresh", connected or (remote and not connecting))
        has_unit = connected and self.unit_list.selected_unit is not None
        for name in self.unit_actions.list_actions():
            self._enable(name, has_unit, self.unit_actions)
        self.host_menu_button.set_visible(remote)

    def _on_scope_changed(self, action, value):
        action.set_state(value)
        self.scope = Scope(value.get_string())
        self.services.set_scope(self.scope)
        manager = self.sessions.get(self.machine_id)
        if manager:
            manager.invalidate_unit_files()
        self.reload(show_spinner=True)

    def _on_show_inactive_changed(self, action, value):
        action.set_state(value)
        self.settings.set_boolean("show-inactive", value.get_boolean())
        self.services.set_empty_hint(self._empty_hint())
        self.reload()

    def _empty_hint(self) -> str:
        if self.show_inactive:
            return ""
        return _("Use “Show Inactive Services” in the main menu to include stopped services.")

    def _on_view_changed(self, action, value):
        action.set_state(value)
        journal = value.get_string() == "journal"
        self.view_stack.set_visible_child_name("journal" if journal else "services")
        self.search_entry.set_placeholder_text(_("Search the journal") if journal else _("Search services"))
        self.search_bar.set_search_mode(False)
        if journal:
            self.journal.show()
        self._update_header()

    @property
    def view(self) -> str:
        return self.lookup_action("view").get_state().get_string()

    def _on_mode_changed(self, action, value):
        action.set_state(value)
        self.settings.set_string("view-mode", value.get_string())
        self._apply_mode()

    @property
    def mode(self) -> str:
        return self.lookup_action("mode").get_state().get_string()

    def _apply_mode(self):
        self.services.set_mode(self.mode)
        self.journal.set_mode(self.mode)
        if self.mode == "advanced":
            self._load_runtime()

    def _update_header(self):
        """Badges, and the title's subtitle for the current machine and view."""
        connected = self.sessions.is_connected(self.machine_id)
        failed = self.services.failed_count if connected else 0
        issues = len(self.journal.issues) if connected else 0
        for badge in (self.failed_badge, self.failed_badge_bottom):
            set_count_badge(badge, failed)
        for badge in (self.issues_badge, self.issues_badge_bottom):
            set_count_badge(badge, issues)
        if not connected or self.content_stack.get_visible_child_name() != "main":
            self.window_title.set_subtitle("")
        elif self.view == "journal":
            self.window_title.set_subtitle(self.journal.summary())
        else:
            scope = _("User services") if self.scope is Scope.USER else _("System services")
            n = len(self.services.units)
            self.window_title.set_subtitle(f"{scope} · " + ngettext("{n} service", "{n} services", n).format(n=n))

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
        if row is None or row.machine_id == self.machine_id and self.services.units:
            return
        self.machine_id = row.machine_id
        self._generation += 1  # results still on their way belong to the previous machine
        self.services.clear()
        self.journal.set_manager(self.sessions.get(self.machine_id))
        host = self._current_host()
        self.window_title.set_title(host.name if host else _("This Computer"))
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
        if self.split_view.get_collapsed():
            self.split_view.set_show_sidebar(False)

    # -- connecting -------------------------------------------------------

    def connect_current(self, secret: str | None = None):
        host = self._current_host()
        if not host or self.sessions.is_connected(host.id) or host.id in self._connecting:
            return
        if host.auth is AuthMethod.PASSWORD and secret is None and not self.sessions.hosts.secret(host):
            self._ask_login_password(host)
            return

        runner = self.sessions.prepare(host, secret)
        self._connecting[host.id] = runner
        self._show_loading(_("Connecting to {host}…").format(host=host.name), cancellable=True)
        self._update_actions()
        run_in_thread(
            self.sessions.connect,
            runner,
            on_done=lambda _m: self._on_connected(host, runner),
            on_error=lambda e: self._on_connect_failed(host, runner, e),
        )

    def cancel_connect(self):
        host = self._current_host()
        runner = self._connecting.pop(host.id, None) if host else None
        if runner:
            # Closes the socket, so the attempt stops instead of finishing in the background.
            runner.cancel()
            self._show_disconnected()
            self._update_actions()

    def _on_connected(self, host, runner):
        if self._connecting.get(host.id) is not runner:
            # Cancelled just as it succeeded.
            self.sessions.disconnect(host.id)
            return
        del self._connecting[host.id]
        self._refresh_row_status(host.id)
        if self.machine_id == host.id:
            self.journal.set_manager(self.sessions.get(host.id))
            self._update_actions()
            self.reload(show_spinner=True)
        else:
            self.toast(_("Connected to {host}").format(host=host.name))

    def _on_connect_failed(self, host, runner, error):
        if self._connecting.get(host.id) is not runner or isinstance(error, ConnectionCancelled):
            return  # cancelled; the UI already moved on
        del self._connecting[host.id]
        visible = self.machine_id == host.id
        if visible:
            self._update_actions()

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
                try:
                    self.sessions.hosts.save(host, password)
                except PilotError as e:
                    self.toast(str(e))  # still connect, just without remembering
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
        self._generation += 1
        self.sessions.disconnect(self.machine_id)
        self._refresh_row_status(self.machine_id)
        self.services.clear()
        self.journal.set_manager(None)
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
        dialog.connect("remove-requested", lambda d, _id: self.remove_host(parent=d, on_removed=d.close))
        dialog.present(self)

    def _on_host_saved(self, _dialog, host_id):
        # Settings may have changed; reconnect with the new ones next time.
        self.sessions.disconnect(host_id)
        self._rebuild_machine_list()
        self.machine_id = None
        self.machine_list.select_row(self._row_for(host_id))

    def remove_host(self, parent=None, on_removed=None):
        host = self._current_host()
        if not host:
            return

        def remove():
            if on_removed:
                on_removed()
            self._remove_host(host.id)

        prompts.confirm(
            parent or self,
            _("Remove {host}?").format(host=host.name),
            _("The host and its saved password will be removed."),
            _("_Remove"),
            remove,
            destructive=True,
        )

    def _remove_host(self, host_id):
        runner = self._connecting.pop(host_id, None)
        if runner:
            runner.cancel()
        self.sessions.disconnect(host_id)
        self.sessions.hosts.remove(host_id)
        self._rebuild_machine_list()
        self.machine_id = None
        self.machine_list.select_row(self.machine_list.get_row_at_index(0))

    # -- units ------------------------------------------------------------

    def reload(self, show_spinner: bool = False, *, refresh_files: bool = False):
        manager = self.sessions.get(self.machine_id)
        if manager is None:
            if self.machine_id != LOCAL_ID and self.machine_id not in self._connecting:
                self.connect_current()
            return
        if refresh_files:
            manager.invalidate_unit_files()
        self._generation += 1
        generation = self._generation
        self._runtime_loaded = False
        machine_id, scope, include_inactive = self.machine_id, self.scope, self.show_inactive
        if show_spinner or not self.services.units:
            self._show_loading(_("Loading services…"))
        self.journal.set_manager(manager)
        # Only refetch the journal when that page is open. Reloading it on every
        # service action was freezing the UI (1 500 entries + full page rebuild).
        if self.view == "journal":
            self.journal.reload()
        else:
            self.journal.mark_stale()

        # list-units is fast. Enable/disabled state is not shown in the list (details
        # dialog loads it). list-unit-files is only needed to add unloaded units when
        # “Show Inactive” is on. Advanced mode then fills PID/memory.
        def done(units):
            if generation != self._generation:
                return
            self._on_units_loaded(units)
            if include_inactive:
                run_in_thread(
                    manager.attach_file_states,
                    units,
                    scope,
                    True,
                    on_done=inactive_done,
                    on_error=incomplete,
                )
            elif self.mode == "advanced":
                self._load_runtime(units, generation)

        def inactive_done(units):
            if generation != self._generation:
                return
            self._on_units_loaded(units)
            if self.mode == "advanced":
                self._load_runtime(units, generation)

        def incomplete(error):
            if generation == self._generation:
                self.toast(_("Could not load inactive services: {error}").format(error=describe(error)))

        def failed(error):
            if generation != self._generation:
                return
            if isinstance(error, ConnectionFailed) and machine_id != LOCAL_ID:
                self.sessions.disconnect(machine_id)
                self.journal.set_manager(None)
                self._refresh_row_status(machine_id)
                self._update_actions()
                self._show_error(_("Connection Lost"), describe(error))
            else:
                self._show_error(_("Could Not Load Services"), describe(error))

        run_in_thread(manager.list_units, scope, include_inactive, on_done=done, on_error=failed)

    def _on_units_loaded(self, units: list[Unit]):
        self.services.set_units(self._carry_over(units))
        self.journal.set_known_units({u.name for u in units})
        self.content_stack.set_visible_child_name("main")
        self.spinner.stop()
        self._update_header()
        self._update_actions()
        self._schedule_journal_badge()

    def _schedule_journal_badge(self) -> None:
        if self._journal_badge_source:
            GLib.source_remove(self._journal_badge_source)
        # Badge only: defer so startup stays focused on the service list.
        self._journal_badge_source = GLib.timeout_add_seconds(45, self._load_journal_badge)

    def _load_journal_badge(self):
        self._journal_badge_source = 0
        if self.sessions.is_connected(self.machine_id) and self.view != "journal" and not self.journal.loaded:
            self.journal.ensure_loaded()
        return GLib.SOURCE_REMOVE

    def _load_runtime(self, units: list[Unit] | None = None, generation: int | None = None) -> None:
        """PID/memory for the advanced table and unit dialog; skipped in simple mode."""
        if self._runtime_loaded:
            return
        manager = self.sessions.get(self.machine_id)
        if manager is None:
            return
        if generation is None:
            generation = self._generation
        scope = self.scope
        payload = units if units is not None else self.services.units

        def runtime_done(completed: list[Unit]):
            if generation == self._generation:
                self._runtime_loaded = True
                self._on_units_loaded(completed)

        def runtime_failed(error):
            if generation == self._generation:
                self.toast(_("Could not load service details: {error}").format(error=describe(error)))

        run_in_thread(manager.add_runtime, payload, scope, on_done=runtime_done, on_error=runtime_failed)

    def _carry_over(self, units: list[Unit]) -> list[Unit]:
        """While startup states and runtime details load, keep the previous ones."""
        previous = {u.name: u for u in self.services.units}
        carried = []
        for unit in units:
            old = previous.get(unit.name)
            if unit.file_state is None and old is not None:
                unit = dataclasses.replace(unit, file_state=old.file_state)
                if old.active_state == unit.active_state:
                    unit = dataclasses.replace(unit, main_pid=old.main_pid, memory=old.memory, since=old.since)
            carried.append(unit)
        return carried

    @Gtk.Template.Callback()
    def on_search_changed(self, entry):
        if self.view == "journal":
            self.journal.set_query(entry.get_text())
        else:
            self.services.set_query(entry.get_text())

    def _on_search_mode(self, bar, _pspec):
        if not bar.get_search_mode():
            self.search_entry.set_text("")

    def control_selected(self, action: UnitAction):
        self.control_unit(self.unit_list.selected_unit, action)

    def control_unit(self, unit: Unit | None, action: UnitAction):
        manager = self.sessions.get(self.machine_id)
        if not unit or not manager:
            return
        scope = self.scope
        def finish():
            if action in (UnitAction.ENABLE, UnitAction.DISABLE):
                manager.invalidate_unit_files()
            self.reload()

        self.operations.run(
            manager,
            lambda: manager.control(unit.name, action, scope),
            on_success=lambda _r: self.toast(self._action_message(unit, action)),
            # Also after a failure or cancel, so a pending enable switch goes back.
            # Also after a failure or cancel (e.g. pending UI), so the list matches reality.
            on_finish=finish,
            error_heading=self._action_error_heading(unit, action),
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

    @staticmethod
    def _action_error_heading(unit: Unit, action: UnitAction) -> str:
        headings = {
            UnitAction.START: _("Could Not Start {unit}"),
            UnitAction.STOP: _("Could Not Stop {unit}"),
            UnitAction.RESTART: _("Could Not Restart {unit}"),
            UnitAction.RELOAD: _("Could Not Reload {unit}"),
            UnitAction.ENABLE: _("Could Not Enable {unit}"),
            UnitAction.DISABLE: _("Could Not Disable {unit}"),
        }
        return headings[action].format(unit=unit.short_name)

    def show_unit(self, unit: Unit | None):
        manager = self.sessions.get(self.machine_id)
        if not unit or not manager:
            return
        dialog = UnitDialog(
            manager,
            unit,
            self.scope,
            self.operations,
            mode=self.lookup_action("mode"),
            on_changed=self.reload,
            action_message=self._action_message,
        )
        dialog.present(self)
        GLib.idle_add(lambda: (dialog.start_loading(), False)[-1])

    def _open_unit_by_name(self, name: str):
        unit = next((u for u in self.services.units if u.name == name), None)
        self.show_unit(unit or Unit(name))

    def _after_unit_created(self, manager: SystemdManager, name: str):
        manager.invalidate_unit_files()
        self.toast(_("Created {unit}").format(unit=name))
        self.reload()

    def _after_daemon_reload(self, manager: SystemdManager):
        manager.invalidate_unit_files()
        self.toast(_("systemd configuration reloaded"))
        self.reload()

    def daemon_reload(self):
        manager = self.sessions.get(self.machine_id)
        if not manager:
            return
        scope = self.scope
        self.operations.run(
            manager,
            lambda: manager.daemon_reload(scope),
            on_success=lambda _r: self._after_daemon_reload(manager),
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
            on_created=lambda name: self._after_unit_created(manager, name),
        )
        dialog.present(self)

    # -- pages ------------------------------------------------------------

    def _show_loading(self, text: str, cancellable: bool = False):
        self.loading_label.set_label(text)
        self.cancel_connect_button.set_visible(cancellable)
        self.spinner.start()
        self.content_stack.set_visible_child_name("loading")
        self._update_header()

    def _show_disconnected(self):
        host = self._current_host()
        self.spinner.stop()
        if host:
            self.disconnected_page.set_description(
                GLib.markup_escape_text(f"{host.username}@{host.hostname}:{host.port}")
            )
        self.content_stack.set_visible_child_name("disconnected")
        self._update_header()

    def _show_error(self, title: str, message: str, offer_edit: bool = False):
        self.spinner.stop()
        self.error_page.set_title(title)
        self.error_page.set_description(GLib.markup_escape_text(message))
        self.error_edit_button.set_visible(offer_edit and self.machine_id != LOCAL_ID)
        self.content_stack.set_visible_child_name("error")
        self._update_header()

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
