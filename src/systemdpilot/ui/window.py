"""Main window: machines in the sidebar, their services or journal in the content pane."""

from __future__ import annotations

import dataclasses

from gi.repository import Adw, Gio, GLib, Gtk, Pango

from .. import APP_ID
from ..core.errors import (
    AuthenticationFailed,
    ConnectionCancelled,
    ConnectionFailed,
    HostKeyMismatch,
    HostKeyUnknown,
    PilotError,
)
from ..core.manager import SystemdManager
from ..core.models import AuthMethod, Host, Unit, UnitAction
from ..core.session import LOCAL_ID, Sessions
from ..core.ssh import SSHRunner
from ..i18n import _, ngettext
from . import prompts, websearch
from .create_unit_dialog import CreateUnitDialog
from .host_dialog import HostDialog
from .journal_details import JournalDetails, JournalDetailsDialog
from .journal_view import JournalView
from .operations import Operations, describe
from .resources import app_icon, template
from .services_view import ServicesView
from .settings import Settings
from .tasks import run_in_thread
from .unit_dialog import UnitDialog, UnitPanel
from .widgets import count_badge, dot, set_count_badge

# The services list beside the details: its header bar needs about this much.
DETAILS_LIST_MIN_WIDTH = 360
# What the window shows, in the order of the sidebar's dropdown.
MODES = ("services", "journal")
MODES_PAGES = ("main", "journal")  # the content stack's page for each


class MachineRow(Gtk.ListBoxRow):
    def __init__(self, machine_id: str, title: str, subtitle: str, icon_name: str):
        super().__init__()
        self.machine_id = machine_id
        self.title, self.subtitle, self.icon_name = title, subtitle, icon_name
        self.connected = False
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
        self.connected = connected
        self.status.set_visible(connected)


@template("window.ui")
class Window(Adw.ApplicationWindow):
    __gtype_name__ = "SystemdPilotWindow"

    toast_overlay: Adw.ToastOverlay = Gtk.Template.Child()
    split_view: Adw.OverlaySplitView = Gtk.Template.Child()
    mode_dropdown: Gtk.DropDown = Gtk.Template.Child()
    filters_bin: Adw.Bin = Gtk.Template.Child()
    # The machine selector at the bottom of the sidebar.
    machine_button: Gtk.MenuButton = Gtk.Template.Child()
    machine_popover: Gtk.Popover = Gtk.Template.Child()
    machine_icon: Gtk.Image = Gtk.Template.Child()
    machine_name: Gtk.Label = Gtk.Template.Child()
    machine_subtitle: Gtk.Label = Gtk.Template.Child()
    machine_dot: Gtk.Box = Gtk.Template.Child()
    machine_list: Gtk.ListBox = Gtk.Template.Child()
    host_menu_button: Gtk.MenuButton = Gtk.Template.Child()
    search_bar: Gtk.SearchBar = Gtk.Template.Child()
    search_entry: Gtk.SearchEntry = Gtk.Template.Child()
    content_stack: Gtk.Stack = Gtk.Template.Child()
    spinner: Gtk.Spinner = Gtk.Template.Child()
    loading_label: Gtk.Label = Gtk.Template.Child()
    cancel_connect_button: Gtk.Button = Gtk.Template.Child()
    services_bin: Adw.Bin = Gtk.Template.Child()
    journal_bin: Adw.Bin = Gtk.Template.Child()
    details_split: Adw.OverlaySplitView = Gtk.Template.Child()
    details_bin: Adw.Bin = Gtk.Template.Child()
    disconnected_page: Adw.StatusPage = Gtk.Template.Child()
    error_page: Adw.StatusPage = Gtk.Template.Child()
    error_edit_button: Gtk.Button = Gtk.Template.Child()

    def __init__(self, *, application: Adw.Application, sessions: Sessions, settings: Settings):
        super().__init__(application=application)
        self.sessions = sessions
        self.settings = settings
        self.machine_id = LOCAL_ID
        self._generation = 0
        # (generation, task) -> quiet: fetches still running; quiet ones don't show the spinner.
        self._pending: dict[tuple[int, str], bool] = {}
        self._journal_badge_source = 0
        self._connecting: dict[str, SSHRunner] = {}  # host id -> connection attempt
        self.operations = Operations(self, self.toast)
        self._details: UnitPanel | None = None  # the panel beside the services list

        self.set_default_size(settings.get_int("window-width"), settings.get_int("window-height"))
        if settings.get_boolean("window-maximized"):
            self.maximize()

        self.services = ServicesView()
        self.services.connect("unit-activated", lambda _v, unit: self.show_unit(unit))
        self.services.connect("unit-action", lambda _v, unit, action: self.control_unit(unit, UnitAction(action)))
        self.services.connect("filter-changed", lambda *_: self._update_badges())
        self.services_bin.set_child(self.services)
        self.filters_bin.set_child(self.services.sidebar)
        self.services.filter_list.connect("row-activated", lambda *_: self._on_filter_activated())
        self._details_placeholder = self._build_details_placeholder()
        self.details_bin.set_child(self._details_placeholder)
        self.details_split.connect("notify::collapsed", self._on_details_collapsed)
        # With the machine list hidden, details take a bigger share of the window.
        for prop in ("notify::show-sidebar", "notify::collapsed"):
            self.split_view.connect(prop, lambda *_: self._fit_details(self.get_width()))
        # The journal takes the place of the services: its filters, list and details.
        journal_operations = Operations(self, self.toast)
        self.journal = JournalView(journal_operations)
        self.journal.connect("open-unit", lambda _v, name: self._open_unit_by_name(name))
        self.journal.connect("changed", lambda *_: self._update_badges())
        self.journal.connect("selected", lambda _v, item: self.journal_details.show(item))
        self.journal.connect("activated", lambda _v, item: self._on_journal_activated(item))
        self.journal.filter_list.connect("row-activated", lambda *_: self._on_filter_activated())
        self.journal_bin.set_child(self.journal)
        journal_operations.parent = self.journal
        self.journal_details = JournalDetails(self.journal)
        button, self._journal_details_badge = self._journal_button()
        self.journal_details.add_header_end(button)
        self.mode_dropdown.connect("notify::selected", lambda d, _p: self._set_mode(MODES[d.get_selected()]))
        self.search_bar.set_key_capture_widget(self.split_view)
        self.search_bar.connect("notify::search-mode-enabled", self._on_search_mode)

        self._setup_actions()
        websearch.install(self)
        self._sync_details_shown()
        self.services.set_empty_hint(self._empty_hint())
        self._rebuild_machine_list()
        self.machine_list.select_row(self.machine_list.get_row_at_index(0))
        # As wide as the selector it opens from; the ellipsized names don't ask for any width.
        self.machine_popover.connect(
            "show", lambda p: p.get_child().set_size_request(max(240, self.machine_button.get_width()), -1)
        )

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

        inactive = Gio.SimpleAction.new_stateful(
            "show-inactive", None, GLib.Variant("b", self.settings.get_boolean("show-inactive"))
        )
        inactive.connect("change-state", self._on_show_inactive_changed)
        self.add_action(inactive)

        add("journal", self.show_journal)
        mode = Gio.SimpleAction.new_stateful("mode", GLib.VariantType.new("s"), GLib.Variant("s", "services"))
        mode.connect("change-state", self._on_mode_changed)
        self.add_action(mode)

        order = self.settings.get_string("unit-label-order")
        if order not in ("description-name", "name-description"):
            order = "name-description"
        label_order = Gio.SimpleAction.new_stateful(
            "unit-label-order", GLib.VariantType.new("s"), GLib.Variant("s", order)
        )
        label_order.connect("change-state", self._on_unit_label_order_changed)
        self.add_action(label_order)
        self.services.set_label_order(order)

    def _enable(self, name, enabled):
        self.lookup_action(name).set_enabled(enabled)

    def _update_actions(self):
        remote = self.machine_id != LOCAL_ID
        connected = self.sessions.is_connected(self.machine_id)
        connecting = self.machine_id in self._connecting
        self._enable("connect", remote and not connected and not connecting)
        self._enable("disconnect", remote and connected)
        self._enable("edit-host", remote)
        self._enable("remove-host", remote)
        for name in ("refresh", "daemon-reload", "create-unit", "show-inactive", "journal"):
            self._enable(name, connected)
        self._enable("refresh", connected or (remote and not connecting))
        self.host_menu_button.set_visible(remote)

    def _on_show_inactive_changed(self, action, value):
        action.set_state(value)
        self.settings.set_boolean("show-inactive", value.get_boolean())
        self.services.set_empty_hint(self._empty_hint())
        self.reload()

    def _empty_hint(self) -> str:
        if self.show_inactive:
            return ""
        return _("Use “Show Inactive Services” in the main menu to include stopped services.")

    def show_journal(self):
        self._set_mode("journal")

    @property
    def journal_shown(self) -> bool:
        return self.lookup_action("mode").get_state().get_string() == "journal"

    def _close_journal(self):
        self._set_mode("services")

    def _set_mode(self, mode: str) -> None:
        self.lookup_action("mode").change_state(GLib.Variant("s", mode))

    def _on_mode_changed(self, action, value):
        mode = value.get_string()
        if mode not in MODES or mode == action.get_state().get_string():
            return
        # Each list starts unsearched; the entry's own update would only reach the new one.
        if self.search_entry.get_text():
            self.search_entry.set_text("")
            self.services.set_query("")
            self.journal.set_query("")
        action.set_state(value)
        journal = mode == "journal"
        self.mode_dropdown.set_selected(MODES.index(mode))
        self.filters_bin.set_child(self.journal.sidebar if journal else self.services.sidebar)
        self.search_entry.set_placeholder_text(_("Search the journal") if journal else _("Search services"))
        if journal:
            self.details_bin.set_child(self.journal_details)
        else:
            self.details_bin.set_child(self._details or self._details_placeholder)
        if self.content_stack.get_visible_child_name() in MODES_PAGES:
            self._show_list()
        if journal:
            self.journal.show()

    def _on_journal_activated(self, item):
        # Beside the list, selecting it already showed it.
        if self.details_split.get_collapsed():
            JournalDetailsDialog(self.journal, item).present(self)

    def _on_unit_label_order_changed(self, action, value):
        action.set_state(value)
        order = value.get_string()
        self.settings.set_string("unit-label-order", order)
        self.services.set_label_order(order)

    def _update_badges(self):
        """The count of journal problems on the Journal buttons."""
        connected = self.sessions.is_connected(self.machine_id)
        issues = len(self.journal.issues) if connected else 0
        tooltip = _("Open system log")
        if issues:
            found = ngettext("{n} problem found", "{n} problems found", issues).format(n=issues)
            tooltip = f"{tooltip}. {found}"
        badges = (
            self._placeholder_journal_badge,
            self._journal_details_badge,
            self._details and self._details.journal_badge,
        )
        for badge in badges:
            if badge:
                set_count_badge(badge, issues)
                badge.get_ancestor(Gtk.Button).set_tooltip_text(tooltip)

    def _journal_button(self) -> tuple[Gtk.Button, Gtk.Label]:
        """For the details column's header bar: one each for the placeholder and the panel."""
        # A bell, with the count of problems on its corner.
        bell = Gtk.Overlay(child=Gtk.Image(icon_name="bell-symbolic"))
        badge = count_badge("error")
        badge.add_css_class("on-icon")
        badge.set_xalign(0.5)
        badge.set_halign(Gtk.Align.END)
        badge.set_valign(Gtk.Align.START)
        badge.set_can_target(False)
        bell.add_overlay(badge)
        button = Gtk.Button(
            child=bell,
            action_name="win.journal",
            tooltip_text=_("Open system log"),
        )
        button.update_property([Gtk.AccessibleProperty.LABEL], [_("Journal")])
        return button, badge

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
            if machine_id == self.machine_id:
                self._show_machine(row)

    def _show_machine(self, row: MachineRow) -> None:
        """The machine selector shows the chosen machine."""
        self.machine_icon.set_from_icon_name(row.icon_name)
        self.machine_name.set_label(row.title)
        self.machine_subtitle.set_label(row.subtitle)
        self.machine_dot.set_visible(row.connected)
        self.machine_button.update_property([Gtk.AccessibleProperty.LABEL], [row.title])

    def _on_filter_activated(self):
        # Over the content on narrow windows, the sidebar gets out of the way after a choice.
        if self.split_view.get_collapsed():
            self.split_view.set_show_sidebar(False)

    def _current_host(self) -> Host | None:
        return self.sessions.hosts.get(self.machine_id)

    @Gtk.Template.Callback()
    def on_machine_selected(self, _listbox, row):
        if row is None or row.machine_id == self.machine_id and self.services.units:
            return
        self.machine_id = row.machine_id
        self._show_machine(row)
        self._generation += 1  # results still on their way belong to the previous machine
        self._update_busy()
        self.services.clear()
        self._close_details()
        self.journal.set_manager(self.sessions.get(self.machine_id))
        host = self._current_host()
        if self.journal_shown:
            self.journal.show()
        if self.sessions.is_connected(self.machine_id):
            self.reload(show_spinner=True)
        elif self.machine_id in self._connecting:
            self._show_loading(_("Connecting to {host}…").format(host=host.name), cancellable=True)
        else:
            self._show_disconnected()
        self._update_actions()

    @Gtk.Template.Callback()
    def on_machine_activated(self, _listbox, row):
        self.machine_popover.popdown()
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
        self._update_busy()
        self.sessions.disconnect(self.machine_id)
        self._refresh_row_status(self.machine_id)
        self.services.clear()
        self.journal.set_manager(None)
        self._close_journal()
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
        machine_id, include_inactive = self.machine_id, self.show_inactive
        # A first load: the list shows as soon as it arrives, and the rest fills in
        # quietly, without the spinner above it.
        quiet = show_spinner or not self.services.units
        if quiet:
            self._show_loading(_("Loading services…"))
        self._set_pending(generation, "list", True)
        self.journal.set_manager(manager)
        # Only refetch the journal when that page is open. Reloading it on every
        # service action was freezing the UI (1 500 entries + full page rebuild).
        if self.journal_shown:
            self.journal.reload()
        else:
            self.journal.mark_stale()

        # list-units is fast. Enable/disabled state is not shown in the list (details
        # dialog loads it). list-unit-files is only needed to add unloaded units when
        # “Show Inactive” is on.
        def done(units):
            if generation != self._generation:
                return
            self._set_pending(generation, "list", False)
            self._on_units_loaded(units)
            if include_inactive:
                self._set_pending(generation, "inactive", True, quiet)
                run_in_thread(
                    manager.attach_file_states,
                    units,
                    True,
                    on_done=inactive_done,
                    on_error=incomplete,
                )

        def inactive_done(units):
            if generation != self._generation:
                return
            self._set_pending(generation, "inactive", False)
            self._on_units_loaded(units)

        def incomplete(error):
            if generation == self._generation:
                self._set_pending(generation, "inactive", False)
                self.toast(_("Could not load inactive services: {error}").format(error=describe(error)))

        def failed(error):
            if generation != self._generation:
                return
            self._set_pending(generation, "list", False)
            if isinstance(error, ConnectionFailed) and machine_id != LOCAL_ID:
                self.sessions.disconnect(machine_id)
                self.journal.set_manager(None)
                self._close_journal()
                self._refresh_row_status(machine_id)
                self._update_actions()
                self._show_error(_("Connection Lost"), describe(error))
            else:
                self._show_error(_("Could Not Load Services"), describe(error))

        run_in_thread(manager.list_services, include_inactive, on_done=done, on_error=failed)

    def _on_units_loaded(self, units: list[Unit]):
        self.services.set_units(self._carry_over(units))
        self.journal.set_known_units({u.name for u in units})
        self._show_list()
        self._update_badges()
        self._update_actions()
        self._schedule_journal_badge()

    def _show_list(self) -> None:
        self.content_stack.set_visible_child_name(MODES_PAGES[self.journal_shown])
        self.spinner.stop()

    def _schedule_journal_badge(self) -> None:
        if self._journal_badge_source:
            GLib.source_remove(self._journal_badge_source)
            self._journal_badge_source = 0
        if self.journal.loaded:
            return
        # Brief pause so list paint (and inactive follow-ups that call
        # here again) finish first. Each reschedule resets the delay, so the
        # badge fetch starts after unit work settles — still off the UI thread.
        self._journal_badge_source = GLib.timeout_add(1500, self._load_journal_badge)

    def _load_journal_badge(self):
        self._journal_badge_source = 0
        if self.sessions.is_connected(self.machine_id) and not self.journal_shown and not self.journal.loaded:
            self.journal.ensure_loaded()
        return GLib.SOURCE_REMOVE

    def _set_pending(self, generation: int, task: str, running: bool, quiet: bool = False) -> None:
        """The spinner above the services list shows while any of them is still fetching."""
        if running:
            self._pending[generation, task] = quiet
        else:
            self._pending.pop((generation, task), None)
        self._update_busy()

    def _update_busy(self) -> None:
        # Fetches of an earlier generation were superseded; their results are dropped.
        self._pending = {p: q for p, q in self._pending.items() if p[0] == self._generation}
        self.services.set_busy(not all(self._pending.values()))

    def _carry_over(self, units: list[Unit]) -> list[Unit]:
        """While startup states and runtime details load, keep the previous ones."""
        previous = {u.key: u for u in self.services.units}
        carried = []
        for unit in units:
            old = previous.get(unit.key)
            if unit.file_state is None and old is not None:
                unit = dataclasses.replace(unit, file_state=old.file_state)
                if old.active_state == unit.active_state:
                    unit = dataclasses.replace(unit, main_pid=old.main_pid, memory=old.memory, since=old.since)
            carried.append(unit)
        return carried

    @Gtk.Template.Callback()
    def on_search_changed(self, entry):
        (self.journal if self.journal_shown else self.services).set_query(entry.get_text())

    def _on_search_mode(self, bar, _pspec):
        if not bar.get_search_mode():
            self.search_entry.set_text("")

    def control_unit(self, unit: Unit | None, action: UnitAction):
        manager = self.sessions.get(self.machine_id)
        if not unit or not manager:
            return

        def finish():
            if action in (UnitAction.ENABLE, UnitAction.DISABLE):
                manager.invalidate_unit_files()
            self.reload()

        self.operations.run(
            manager,
            lambda: manager.control(unit.name, action, unit.scope),
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
        """Beside the services list, or in a dialog when the window is too narrow."""
        manager = self.sessions.get(self.machine_id)
        if not unit or not manager:
            return
        host = self._current_host()
        options = dict(
            machine_label=host.name if host else _("This Computer"),
            on_changed=self.reload,
            action_message=self._action_message,
        )
        self.services.select_unit(unit)
        if not self.details_split.get_collapsed():
            if self._details:
                self._details.discard()
            panel = UnitPanel(manager, unit, unit.scope, self.operations, **options)
            button, panel.journal_badge = self._journal_button()
            panel.add_header_end(button)
            self._update_badges()
            self._details = panel
            self.details_bin.set_child(panel)
            panel.start_loading()
            return
        dialog = UnitDialog(manager, unit, unit.scope, self.operations, **options)
        dialog.present(self)
        GLib.idle_add(lambda: (dialog.start_loading(), False)[-1])

    def _build_details_placeholder(self) -> Gtk.Widget:
        header = Adw.HeaderBar(show_title=False)  # keeps the window buttons on this side
        button, self._placeholder_journal_badge = self._journal_button()
        header.pack_end(button)
        view = Adw.ToolbarView()
        view.add_top_bar(header)
        page = Adw.StatusPage(
            description=_("Select a service to display its details."),
        )
        icon = app_icon(128)
        if icon:
            page.set_paintable(icon)
        else:
            page.set_icon_name(APP_ID)
        view.set_content(page)
        return view

    def _sync_details_shown(self):
        """The details column sits beside the list when there is room."""
        split = self.details_split
        split.set_show_sidebar(not split.get_collapsed())

    def _close_details(self):
        if self._details:
            self._details.discard()
            self._details = None
        self.services.select_unit(None)
        if not self.journal_shown:
            self.details_bin.set_child(self._details_placeholder)

    def _on_details_collapsed(self, split, _pspec):
        # Too narrow for two columns: the open service moves to a dialog, unless the journal is shown.
        if split.get_collapsed() and self._details and self.journal_shown:
            self._close_details()
        elif split.get_collapsed() and self._details:
            unit = self._details.unit
            self._close_details()
            self.show_unit(unit)
        self._sync_details_shown()

    def _fit_details(self, width: int):
        """Details take 60% of the whole window, 70% with the machine list hidden.

        The split view's fraction is of its own width, which excludes the machine
        list, so it is worked out from the window width. The services list keeps
        room for its header bar, so on smaller windows details get less.
        """
        if width <= 0:
            return
        split = self.split_view
        machines = 0
        if split.get_show_sidebar() and not split.get_collapsed():
            share = width * split.get_sidebar_width_fraction()
            machines = min(max(share, split.get_min_sidebar_width()), split.get_max_sidebar_width())
        available = width - machines
        if available <= 0:
            return
        details = min((0.6 if machines else 0.7) * width, available - DETAILS_LIST_MIN_WIDTH)
        fraction = max(details / available, 0)
        if abs(fraction - self.details_split.get_sidebar_width_fraction()) > 0.001:
            self.details_split.set_sidebar_width_fraction(fraction)

    def _open_unit_by_name(self, name: str):
        """From the journal: back to the services, with that one open."""
        unit = next((u for u in self.services.units if u.name == name), None)
        if isinstance(dialog := self.get_visible_dialog(), JournalDetailsDialog):
            dialog.close()
        self._close_journal()
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
        self.operations.run(
            manager,
            manager.daemon_reload_all,
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
            None,
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
        self._update_badges()

    def _show_disconnected(self):
        host = self._current_host()
        self.spinner.stop()
        if host:
            self.disconnected_page.set_description(
                GLib.markup_escape_text(f"{host.username}@{host.hostname}:{host.port}")
            )
        self.content_stack.set_visible_child_name("disconnected")
        self._update_badges()

    def _show_error(self, title: str, message: str, offer_edit: bool = False):
        self.spinner.stop()
        self.error_page.set_title(title)
        self.error_page.set_description(GLib.markup_escape_text(message))
        self.error_edit_button.set_visible(offer_edit and self.machine_id != LOCAL_ID)
        self.content_stack.set_visible_child_name("error")
        self._update_badges()

    def toast(self, message: str):
        self.toast_overlay.add_toast(Adw.Toast(title=message, use_markup=False, timeout=3))

    # -- lifecycle --------------------------------------------------------

    def do_size_allocate(self, width: int, height: int, baseline: int):
        self._fit_details(width)
        Adw.ApplicationWindow.do_size_allocate(self, width, height, baseline)

    def do_close_request(self):
        if not self.is_maximized():
            width, height = self.get_default_size()
            self.settings.set_int("window-width", width)
            self.settings.set_int("window-height", height)
        self.settings.set_boolean("window-maximized", self.is_maximized())
        return False
