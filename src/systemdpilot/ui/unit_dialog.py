"""Details, logs and controls for one unit, as a side panel or a dialog."""

from __future__ import annotations

import itertools
import re
from collections.abc import Callable
from dataclasses import dataclass

from gi.repository import Adw, GLib, Gtk, Pango

from ..core.journal import matches_query
from ..core.manager import SystemdManager
from ..core.models import LogEntry, LogResult, Scope, Unit, UnitAction, short_unit_name
from ..core.parsers import (
    override_body,
    parse_int,
    parse_timer_specs,
    parse_unit_list,
    parse_unix_timestamp,
    unit_file_body,
)
from ..core.ssh import SSHRunner
from ..i18n import _
from . import prompts, text, widgets, words
from .create_unit_dialog import CreateUnitDialog
from .operations import Operations, describe
from .resources import template
from .tasks import run_in_thread

_ACTIVITY_LIMIT = 300
_ACTIVITY_CHUNK = 25
# Just after GTK redraws (GDK_PRIORITY_REDRAW), ahead of text view validation.
_AFTER_REDRAW = GLib.PRIORITY_HIGH_IDLE + 21

_EXEC_PATH_RE = re.compile(r"path=(\S+)")
_EXEC_ARGV_RE = re.compile(r"argv\[\]=(.*?) ; ignore_errors=")
_FRACTION_RE = re.compile(r"(\d+)\.\d+s\b")
# Types that run processes in a control group of their own while active.
_RESOURCE_TYPES = ("service", "slice", "scope")
# Types without unit files to enable: the kernel or a program makes them.
_NO_STARTUP_TYPES = ("device", "scope")
# Unit names listed in a row before "…and N more".
_UNIT_LIST_LIMIT = 8
# The signal menu's items: label, signal, and whether it is destructive.
_SIGNAL_ITEMS = (
    (_("Terminate"), "SIGTERM", False),
    (_("Hang Up"), "SIGHUP", False),
    (_("Kill…"), "SIGKILL", True),
)


@dataclass
class _Details:
    unit: Unit
    properties: dict[str, str]
    unit_file: str
    logs: LogResult


@template("unit-dialog.ui")
class UnitPanel(Adw.BreakpointBin):
    """Shown beside the services list, or inside :class:`UnitDialog`."""

    __gtype_name__ = "SystemdPilotUnitPanel"

    toast_overlay: Adw.ToastOverlay = Gtk.Template.Child()
    header_bar: Adw.HeaderBar = Gtk.Template.Child()
    icon_tile: Gtk.Box = Gtk.Template.Child()
    title_label: Gtk.Label = Gtk.Template.Child()
    name_label: Gtk.Label = Gtk.Template.Child()
    action_box: Gtk.Box = Gtk.Template.Child()
    stack: Adw.ViewStack = Gtk.Template.Child()
    loading_stack: Gtk.Stack = Gtk.Template.Child()
    pages_spinner: Gtk.Spinner = Gtk.Template.Child()
    overview_page: Adw.ViewStackPage = Gtk.Template.Child()
    activity_page: Adw.ViewStackPage = Gtk.Template.Child()
    overview_box: Gtk.Box = Gtk.Template.Child()
    log_search: Gtk.SearchEntry = Gtk.Template.Child()
    raw_switch: Gtk.Switch = Gtk.Template.Child()
    activity_stack: Gtk.Stack = Gtk.Template.Child()
    activity_box: Gtk.Box = Gtk.Template.Child()
    logs_banner: Adw.Banner = Gtk.Template.Child()
    logs_view: Gtk.TextView = Gtk.Template.Child()
    logs_scroll: Gtk.ScrolledWindow = Gtk.Template.Child()

    def __init__(
        self,
        manager: SystemdManager,
        unit: Unit,
        scope: Scope,
        operations: Operations,
        *,
        machine_label: str = "",
        on_changed: Callable[[], None],
        action_message: Callable[[Unit, UnitAction], str],
    ):
        super().__init__()
        self.manager = manager
        self.unit = unit
        self.scope = scope
        self.operations = operations
        self._machine_label = machine_label or _("This Computer")
        self._on_changed = on_changed
        self._action_message = action_message
        self._details: _Details | None = None
        self._pending_activity: _Details | None = None  # loaded, its rows not built yet
        self._activity_gen = 0
        self._log_query = ""  # lower case; the Log page shows only the entries it finds
        self._closed = False
        # On remote hosts, logs the SSH user may not read can be read through sudo.
        self._remote = isinstance(manager.runner, SSHRunner)
        self._elevated = False
        self._loads = 0
        self._details_load: int | None = None  # the load the shown details came from
        self.journal_badge: Gtk.Label | None = None  # on the window's Journal button, beside the list
        self.logs_banner.connect("button-clicked", lambda *_: self._view_logs_as_admin())
        self.raw_switch.connect("notify::active", self._on_raw_toggled)
        self.log_search.connect("search-changed", self._on_log_search)
        self.stack.connect("notify::visible-child-name", lambda *_: self._on_page_changed())

        self._show_unit(unit)

    def start_loading(self) -> None:
        """Fetch fresh data in the background (call after :meth:`present`)."""
        self.load()

    def still_loading(self) -> bool:
        """The details spinner is up, so systemd should stay free for this fetch."""
        return not self._closed and self.loading_stack.get_visible_child_name() != "pages"

    def discard(self) -> None:
        """No longer shown: results still on their way are dropped."""
        self._closed = True

    def add_header_end(self, widget: Gtk.Widget) -> None:
        """A button of the window's, placed before the close button."""
        self.header_bar.pack_end(widget)

    def _on_raw_toggled(self, switch, _pspec):
        raw = switch.get_active()
        self.activity_stack.set_visible_child_name("raw" if raw else "list")
        # The text view lays out every line. Filling it while it is hidden
        # keeps the main loop busy long after the overview is on screen.
        if raw and self._details is not None:
            self._show_raw_logs(self._details)

    def _on_log_search(self, entry):
        self._log_query = entry.get_text().strip().lower()
        if self._details is not None:
            if self.raw_switch.get_active():
                self._show_raw_logs(self._details)
            if self._pending_activity is None:  # else it is built with the query when shown
                self._build_activity(self._details)

    def _searched_logs(self, details: _Details) -> list[LogEntry]:
        """The unit's entries the log search finds, oldest first."""
        return [e for e in details.logs.entries if matches_query(e, self._log_query)]

    def _show_raw_logs(self, details: _Details) -> None:
        entries = self._searched_logs(details)
        text.set_logs(self.logs_view.get_buffer(), entries)
        if not entries:
            message = widgets.no_results(self._log_query) if self._log_query else _("No log entries.")
            self.logs_view.get_buffer().set_text(message)
        GLib.idle_add(self._scroll_logs_to_end)

    # -- header -----------------------------------------------------------

    def _show_unit(self, unit: Unit, props: dict[str, str] | None = None):
        self.unit = unit
        self.title_label.set_label(unit.short_name)
        self.title_label.set_tooltip_text(unit.name)
        description = words.unit_description(unit)
        self.name_label.set_label(description)
        self.name_label.set_visible(bool(description))
        self.name_label.set_tooltip_text(description or None)
        self.icon_tile.set_css_classes(["unit-icon", words.state_css(unit)])
        self.icon_tile.get_first_child().set_from_icon_name(words.TYPE_ICONS.get(unit.type, "pegged-wheels-symbolic"))
        self._show_actions(unit, props)

    def _show_actions(self, unit: Unit, props: dict[str, str] | None):
        widgets.clear(self.action_box)
        buttons = {
            UnitAction.START: (_("_Start"), "media-playback-start-symbolic", "suggested-action"),
            UnitAction.RELOAD: (_("Re_load"), "emblem-synchronizing-symbolic", None),
            UnitAction.RESTART: (_("_Restart"), "view-refresh-symbolic", None),
            UnitAction.STOP: (_("S_top"), "media-playback-stop-symbolic", "destructive-action"),
        }
        controls = Gtk.Box(spacing=10, halign=Gtk.Align.CENTER)
        for action in words.detail_actions(unit, props):
            label, icon, css = buttons[action]
            button = Gtk.Button(
                child=Adw.ButtonContent(label=label, icon_name=icon, use_underline=True),
                css_classes=["hero-action"],
            )
            if css:
                button.add_css_class(css)
            button.connect("clicked", lambda _b, a=action: self._on_control_clicked(a))
            controls.append(button)
        # The signal menu sits at the row's end, the controls stay centered.
        row = Gtk.CenterBox(center_widget=controls)
        if props and words.can_signal(unit, props):
            row.set_end_widget(self._signal_button())
        if controls.get_first_child() or row.get_end_widget():
            self.action_box.append(row)
        if props and (extra := self._extra_actions(unit, props)).get_first_child():
            self.action_box.append(extra)

    def _extra_actions(self, unit: Unit, props: dict[str, str]) -> Gtk.Box:
        """What the unit allows beyond starting and stopping it, in a smaller row."""
        box = Gtk.Box(spacing=8, halign=Gtk.Align.CENTER)

        def add(label: str, callback: Callable[[], None], tooltip: str = "", destructive: bool = False) -> None:
            button = Gtk.Button(label=label, tooltip_text=tooltip or None, css_classes=["small-pill"])
            if destructive:
                button.add_css_class("destructive-action")
            button.connect("clicked", lambda *_: callback())
            box.append(button)

        if unit.type == "timer":
            for name in parse_unit_list(props.get("Triggers", "")):
                label = _("Run {unit} Now").format(unit=short_unit_name(name))
                add(label, lambda n=name: self._run_action(UnitAction.START, n))
        if unit.is_failed:
            add(_("Clear Failed State"), lambda: self._run_action(UnitAction.RESET_FAILED))
        return box

    def _signal_button(self) -> Gtk.MenuButton:
        """⋯ with a menu that sends a signal to the unit's processes."""
        items = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, css_classes=["signal-menu"])
        items.append(widgets.label(_("Send signal"), "heading", "dim-label", "signal-menu-title"))
        popover = Gtk.Popover(child=items, has_arrow=False, halign=Gtk.Align.END)
        for label, signal, destructive in _SIGNAL_ITEMS:
            line = Gtk.Box(spacing=32)
            line.append(widgets.label(label, hexpand=True))
            line.append(widgets.label(signal, "signal-name", "dim-label"))
            item = Gtk.Button(child=line, css_classes=["flat", "signal-item"])
            if destructive:
                item.add_css_class("error")
            item.connect("clicked", lambda _b, s=signal: (popover.popdown(), self._on_signal(s)))
            items.append(item)
        return Gtk.MenuButton(
            icon_name="view-more-horizontal-symbolic",
            popover=popover,
            valign=Gtk.Align.CENTER,
            tooltip_text=_("Send Signal"),
        )

    def _on_control_clicked(self, action: UnitAction):
        if question := words.confirm_control(self.unit, action):
            heading, body, label = question
            prompts.confirm(self, heading, body, label, lambda: self._run_action(action), destructive=True)
        else:
            self._run_action(action)

    def _set_busy(self, busy: bool):
        self.action_box.set_sensitive(not busy)
        self.overview_box.set_sensitive(not busy)

    def _show_pages(self) -> None:
        """Everything has loaded (or failed): the spinner gives way to the pages, once."""
        self.pages_spinner.stop()
        if self.loading_stack.get_visible_child_name() != "pages":
            self.loading_stack.set_visible_child_name("pages")
            self._focus_log_search()

    def _on_page_changed(self) -> None:
        if self.stack.get_visible_child_name() == "activity":
            self._build_pending_activity()
            self._focus_log_search()

    def _focus_log_search(self) -> None:
        """On the Log page, typing searches it."""
        if self.stack.get_visible_child_name() == "activity":
            self.log_search.grab_focus()

    # -- loading ----------------------------------------------------------

    def load(self):
        manager, name, scope = self.manager, self.unit.name, self.scope
        self._loads += 1
        load = self._loads

        def fetch_core() -> tuple[dict[str, str], Unit]:
            props = manager.properties(name, scope)
            fresh = Unit(
                name=name,
                description=props.get("Description", self.unit.description),
                load_state=props.get("LoadState", ""),
                active_state=props.get("ActiveState", ""),
                sub_state=props.get("SubState", ""),
                file_state=props.get("UnitFileState", ""),
                scope=scope,
            )
            return props, manager.add_runtime([fresh])[0]

        def core_done(result: tuple[dict[str, str], Unit]):
            if self._closed or load != self._loads:
                return
            props, unit = result
            # Shown with the rest, so the pages don't fill in twice.
            self._load_heavy(props, unit, load)

        def core_failed(error):
            if self._closed or load != self._loads:
                return
            self._on_load_failed(error)

        if self._elevated:
            self._load_heavy_elevated(load)
            return

        run_in_thread(
            fetch_core,
            on_done=core_done,
            on_error=core_failed,
            priority=GLib.PRIORITY_DEFAULT,
        )

    def _load_heavy(self, props: dict[str, str], unit: Unit, load: int) -> None:
        manager, name, scope, elevated = self.manager, self.unit.name, self.scope, self._elevated

        def fetch_heavy() -> tuple[str, LogResult]:
            return manager.unit_file(name, scope), manager.logs(name, scope, privileged=elevated)

        def heavy_done(result: tuple[str, LogResult]):
            if self._closed or load != self._loads:
                return
            unit_file, logs = result
            self._on_loaded(_Details(unit=unit, properties=props, unit_file=unit_file, logs=logs), load)

        def heavy_failed(error):
            if self._closed or load != self._loads:
                return
            self._on_load_failed(error)

        run_in_thread(
            fetch_heavy,
            on_done=heavy_done,
            on_error=heavy_failed,
            priority=GLib.PRIORITY_DEFAULT,
        )

    def _load_heavy_elevated(self, load: int) -> None:
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
                scope=scope,
            )
            unit = manager.add_runtime([unit])[0]
            return _Details(
                unit=unit,
                properties=props,
                unit_file=manager.unit_file(name, scope),
                logs=manager.logs(name, scope, privileged=True),
            )

        def finished():
            if not self._closed and load == self._loads and self._details_load != load:
                self._elevated = False
                self.load()

        self.operations.run(
            manager,
            fetch,
            on_success=lambda details: self._on_loaded(details, load),
            on_finish=finished,
            error_heading=_("Could Not Read the Logs as Administrator"),
        )

    def _view_logs_as_admin(self):
        self._elevated = True
        self.load()

    def _on_loaded(self, details: _Details, load: int | None = None):
        if self._closed:
            return
        self._details_load = load
        self._details = details
        self._show_unit(details.unit, details.properties)
        if self.raw_switch.get_active():
            self._show_raw_logs(details)
        self.logs_banner.set_title(GLib.markup_escape_text(self._logs_warning(details)))
        self.logs_banner.set_button_label(_("View as Administrator") if self._remote else None)
        self.logs_banner.set_revealed(bool(details.logs.warning))
        self._build_overview(details)
        # Activity rows and the raw text view are built when that page is shown.
        # Doing it up front, for a log the overview does not display, held the
        # main loop for over a second and delayed the next service's details.
        self._pending_activity = details
        self._show_pages()
        if self.stack.get_visible_child_name() != "activity":
            return
        clock = self.get_frame_clock()
        if clock is None:
            self._queue_activity()
        else:
            handler = clock.connect("after-paint", lambda c: (c.disconnect(handler), self._queue_activity()))

    def _queue_activity(self) -> None:
        # Ahead of GTK's own idle work: text views validating hundreds of log
        # lines would otherwise hold it up for seconds.
        GLib.idle_add(lambda: self._build_pending_activity() and False, priority=GLib.PRIORITY_HIGH_IDLE)

    def _build_pending_activity(self) -> None:
        details, self._pending_activity = self._pending_activity, None
        if details and not self._closed:
            self._build_activity(details)

    def _scroll_logs_to_end(self):
        adj = self.logs_scroll.get_vadjustment()
        adj.set_value(adj.get_upper() - adj.get_page_size())
        return GLib.SOURCE_REMOVE

    def _on_load_failed(self, error):
        if self._closed:
            return
        message = describe(error)
        widgets.clear(self.overview_box)
        self.overview_box.append(
            Adw.StatusPage(
                icon_name="dialog-warning-symbolic",
                title=_("Could Not Load Details"),
                description=GLib.markup_escape_text(message),
            )
        )
        self.toast_overlay.add_toast(Adw.Toast(title=_("Could not load details"), timeout=3))
        self._show_pages()

    # -- pages ------------------------------------------------------------

    def _build_overview(self, details: _Details):
        unit, props = details.unit, details.properties
        box = self.overview_box
        widgets.clear(box)

        if unit.is_failed and details.logs.entries:
            box.append(self._error_banner(details))
        elif unit.is_failed:
            banner = Gtk.Box(spacing=14, css_classes=["error-banner"])
            banner.append(Gtk.Image(icon_name="dialog-error-symbolic", css_classes=["error"]))
            banner.append(widgets.label("failed", "heading", "error"))
            box.append(banner)

        status = Adw.PreferencesGroup(title=_("Status"))
        status.add(self._state_row(unit))
        load = props.get("LoadState") or unit.load_state or "—"
        status.add(self._row("Load State", load))
        if unit.kind == "running" and unit.main_pid:
            program = (props.get("ExecMainPath") or self._program(props)).rsplit("/", 1)[-1] or unit.short_name
            pid_row = self._row(_("Main PID"), f"{unit.main_pid} ({program})")
            pid_row.set_tooltip_text("MainPID")
            status.add(pid_row)
        box.append(status)

        if kind_group := self._type_group(unit, props):
            box.append(kind_group)

        behavior = Adw.PreferencesGroup(title=_("Startup"), visible=unit.type not in _NO_STARTUP_TYPES)
        if words.can_toggle_startup(unit):
            enabled = words.starts_at_boot(unit)
            row = Adw.SwitchRow(
                title=_("Enabled"),
                subtitle=words.enable_caption(unit) if enabled else "",
                active=enabled,
            )
            if unit.file_state not in ("enabled", "disabled"):
                # The switch says enabled or not; only states like enabled-runtime say more.
                row.add_suffix(widgets.label(unit.file_state or "", "dim-label", "caption"))
            row.connect("notify::active", self._on_startup_toggled)
            behavior.add(row)
        else:
            state = unit.file_state or ""
            row = self._row(_("Unit file state"), words.file_state_word(state), words.file_state_caption(state))
            if tip := words.file_state_tooltip(state):
                row.set_tooltip_text(tip)
            behavior.add(row)
        if words.can_unmask(unit):
            behavior.add(self._mask_row(_("Unmask"), UnitAction.UNMASK, destructive=False))
        elif words.can_mask(unit):
            behavior.add(self._mask_row(_("Mask"), UnitAction.MASK, destructive=True))
        box.append(behavior)

        if unit.kind == "running" and unit.type in _RESOURCE_TYPES:
            resources = Adw.PreferencesGroup(title=_("Resources"))
            memory = parse_int(props.get("MemoryCurrent", ""))
            peak = parse_int(props.get("MemoryPeak", ""))
            memory_row = self._row(
                "MemoryCurrent",
                words.size(memory),
                _("peak {size}").format(size=words.size(peak)) if peak else "",
            )
            if peak:
                memory_row.set_tooltip_text(f"MemoryPeak {words.size(peak)}")
            resources.add(memory_row)
            resources.add(self._row("CPUUsageNSec", words.cpu_time(parse_int(props.get("CPUUsageNSec", "")))))
            tasks = parse_int(props.get("TasksCurrent", ""))
            limit = parse_int(props.get("TasksMax", ""))
            tasks_row = self._row(
                "TasksCurrent",
                str(tasks) if tasks is not None else "—",
                f"TasksMax {limit:,}" if limit else "",
            )
            resources.add(tasks_row)
            box.append(resources)

        where = Adw.PreferencesGroup(title=_("Unit"))
        if unit.is_user:
            where.add(self._row(_("Scope"), unit.scope.value, "systemctl --user"))
        else:
            where.add(self._row(_("Scope"), unit.scope.value, "systemctl --system"))
        path = props.get("FragmentPath") or ""
        file_row = self._row(_("Unit file"), path or "—", "FragmentPath" if path else "", mono=True)
        if words.can_override(unit, props):
            override = Gtk.Button(
                label=_("_Override"),
                use_underline=True,
                valign=Gtk.Align.CENTER,
                tooltip_text=_("Change some settings in a drop-in file, as systemctl edit does"),
                css_classes=["small-pill"],
            )
            override.connect("clicked", lambda *_: self._edit_override())
            file_row.add_suffix(override)
        if path and unit_file_body(details.unit_file):
            edit = Gtk.Button(
                label=_("_Edit"),
                use_underline=True,
                valign=Gtk.Align.CENTER,
                tooltip_text=_("Replace the whole unit file"),
                css_classes=["small-pill"],
            )
            edit.connect("clicked", lambda *_: self._edit_unit_file())
            file_row.add_suffix(edit)
        where.add(file_row)
        drop_ins = (props.get("DropInPaths") or "").split()
        if drop_ins:
            where.add(self._row(_("Drop-ins"), "\n".join(drop_ins), "DropInPaths", mono=True))
        box.append(where)

        recent = [e for e in reversed(details.logs.entries[-3:])]
        activity = Adw.PreferencesGroup(title=_("Recent log"))
        more = Gtk.Button(label=_("Full Log"), css_classes=["flat"], valign=Gtk.Align.CENTER)
        more.connect("clicked", lambda *_: self.stack.set_visible_child_name("activity"))
        activity.set_header_suffix(more)
        listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, css_classes=["boxed-list"])
        for entry in recent:
            listbox.append(self._recent_row(entry))
        if not recent:
            listbox.append(widgets.placeholder_row(_("Nothing logged yet")))
        activity.add(listbox)
        box.append(activity)

    def _error_banner(self, details: _Details) -> Gtk.Widget:
        banner = Gtk.Box(spacing=14, css_classes=["error-banner"])
        banner.append(Gtk.Image(icon_name="dialog-error-symbolic", valign=Gtk.Align.START, css_classes=["error"]))
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, hexpand=True)
        texts.append(widgets.label("failed", "heading", "error"))
        errors = [e for e in details.logs.entries if e.priority <= 3]
        reason = errors[-1].message if errors else details.properties.get("Result", "")
        if reason:
            texts.append(widgets.label(reason, "issue-explanation", wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR))
        banner.append(texts)
        button = Gtk.Button(label=_("Journal"), valign=Gtk.Align.START, css_classes=["small-pill"])
        button.connect("clicked", lambda *_: self.stack.set_visible_child_name("activity"))
        banner.append(button)
        return banner

    def _build_activity(self, details: _Details):
        box = self.activity_box
        widgets.clear(box)
        if details.logs.warning:
            notice = Gtk.Box(spacing=12, margin_bottom=8)
            notice.append(widgets.label(self._logs_warning(details), "dim-label", "caption", wrap=True, hexpand=True))
            if self._remote:
                button = Gtk.Button(label=_("View as Administrator"), css_classes=["small-pill"])
                button.connect("clicked", lambda *_: self._view_logs_as_admin())
                notice.append(button)
            box.append(notice)
        entries = list(reversed(self._searched_logs(details)))[:_ACTIVITY_LIMIT]
        section = widgets.Section(_("Log"), _("Newest first"))
        if not entries:
            message = widgets.no_results(self._log_query) if self._log_query else _("No log entries.")
            section.list.append(widgets.placeholder_row(message))
        box.append(section.box)
        # A screenful at once, the rest in batches between frames: building
        # hundreds of rows in one go froze the window for half a second.
        self._activity_gen += 1
        gen = self._activity_gen
        rows = iter(entries)

        def add_rows() -> bool:
            if gen != self._activity_gen or self._closed:
                return GLib.SOURCE_REMOVE
            batch = list(itertools.islice(rows, _ACTIVITY_CHUNK))
            for entry in batch:
                section.list.append(widgets.log_row(entry, show_source=False))
            return GLib.SOURCE_CONTINUE if len(batch) == _ACTIVITY_CHUNK else GLib.SOURCE_REMOVE

        if add_rows():
            GLib.idle_add(add_rows, priority=_AFTER_REDRAW)

    def _logs_warning(self, details: _Details) -> str:
        if self._remote:
            return _("Some entries may be hidden: only entries this user may read are shown.")
        return details.logs.warning

    def _type_group(self, unit: Unit, props: dict[str, str]) -> Adw.PreferencesGroup | None:
        """What this type of unit does: a timer's schedule, a socket's addresses, a mount's device…"""
        get = props.get
        rows: list[Adw.ActionRow] = []
        title = ""
        if unit.type == "service":
            title = _("Service")
            if get("Type"):
                rows.append(self._row("Type", get("Type")))
            if commands := self._commands(props):
                command_row = self._row("ExecStart", commands, mono=True)
                command_row.add_css_class("command-row")
                rows.append(command_row)
        elif unit.type == "timer":
            title = _("Schedule")
            specs = [*parse_timer_specs(get("TimersCalendar", "")), *parse_timer_specs(get("TimersMonotonic", ""))]
            rows.append(self._row(_("Next run"), self._next_run(props, specs), "NextElapseUSecRealtime"))
            last = parse_unix_timestamp(get("LastTriggerUSec", ""))
            rows.append(self._row(_("Last run"), words.moment(last) if last else _("never"), "LastTriggerUSec"))
            for setting, value, _next in specs:
                rows.append(self._row(setting.replace("USec", "Sec"), value))
            if get("Persistent") == "yes":
                rows.append(self._row("Persistent", "yes", _("runs missed while off are caught up")))
        elif unit.type == "socket":
            title = _("Listening")
            rows += [self._row("Listen", listen, mono=True) for listen in get("Listen", "").splitlines()]
            if get("Accept") == "yes":
                rows.append(self._row("NConnections", get("NConnections", "0"), _("open now")))
                rows.append(self._row("NAccepted", get("NAccepted", "0"), _("since started")))
        elif unit.type == "path":
            title = _("Watching")
            rows += [self._row("Paths", path, mono=True) for path in get("Paths", "").splitlines()]
        elif unit.type in ("mount", "swap"):
            title = _("Mount") if unit.type == "mount" else _("Swap")
            for key in ("What", "Where", "Type", "Options", "Priority"):
                if get(key):
                    rows.append(self._row(key, get(key), mono=key in ("What", "Where")))
        elif unit.type == "automount":
            title = _("Automount")
            if get("Where"):
                rows.append(self._row("Where", get("Where"), mono=True))
            idle = get("TimeoutIdleUSec", "")
            rows.append(
                self._row("TimeoutIdleSec", idle if idle not in ("", "0") else _("never"), _("unmount when idle"))
            )
        elif unit.type == "device":
            title = _("Device")
            if get("SysFSPath"):
                rows.append(self._row("SysFSPath", get("SysFSPath"), mono=True))
        elif unit.type in ("slice", "scope"):
            title = _("Control Group")
            if get("ControlGroup"):
                rows.append(self._row("ControlGroup", get("ControlGroup"), mono=True))
        elif unit.type == "target":
            title = _("Dependencies")
            for key in ("Requires", "Wants"):
                if row := self._units_row(key, get(key, "")):
                    rows.append(row)
        # What starts what: a timer, socket or path starts a service; a service may be started by one.
        for key in ("Triggers", "TriggeredBy"):
            if row := self._units_row(key, get(key, "")):
                rows.append(row)
        if not rows:
            return None
        group = Adw.PreferencesGroup(title=title)
        for row in rows:
            group.add(row)
        return group

    @staticmethod
    def _next_run(props: dict[str, str], specs: list[tuple[str, str, str]]) -> str:
        if when := parse_unix_timestamp(props.get("NextElapseUSecRealtime", "")):
            return words.moment(when)
        # Timers counted from boot or from their last run give the time left instead.
        spans = [_FRACTION_RE.sub(r"\1s", next_elapse) for _s, _v, next_elapse in specs if next_elapse not in ("", "0")]
        if spans and not spans[0].startswith("@"):
            return _("in {time}").format(time=spans[0])
        return "—"

    def _units_row(self, key: str, value: str) -> Adw.ActionRow | None:
        names = parse_unit_list(value)
        if not names:
            return None
        shown = "\n".join(short_unit_name(n) for n in names[:_UNIT_LIST_LIMIT])
        if len(names) > _UNIT_LIST_LIMIT:
            shown += "\n" + _("…and {n} more").format(n=len(names) - _UNIT_LIST_LIMIT)
        return self._row(key, shown, mono=True)

    @staticmethod
    def _program(props: dict[str, str]) -> str:
        match = _EXEC_PATH_RE.search(props.get("ExecStart", ""))
        return match.group(1) if match else ""

    @staticmethod
    def _commands(props: dict[str, str]) -> str:
        """ExecStart= as systemd holds it: each command with its arguments, one per line."""
        return "\n".join(_EXEC_ARGV_RE.findall(props.get("ExecStart", "")))

    @staticmethod
    def _state_row(unit: Unit) -> Adw.ActionRow:
        sentence = words.state_sentence(unit)
        row = Adw.ActionRow(title="Active State", subtitle=sentence)
        state = Gtk.Box(spacing=8, valign=Gtk.Align.CENTER)
        state.append(widgets.dot(unit.kind))
        state.append(widgets.label(unit.state_label, "heading", words.state_css(unit)))
        row.add_suffix(state)
        return row

    def _mask_row(self, label: str, action: UnitAction, *, destructive: bool) -> Adw.ActionRow:
        row = Adw.ActionRow(title=label, subtitle="systemctl " + action.value)
        button = Gtk.Button(
            label=label,
            valign=Gtk.Align.CENTER,
            css_classes=["destructive-action", "small-pill"] if destructive else ["small-pill"],
        )
        button.connect("clicked", lambda *_: self._on_mask_action(action))
        row.add_suffix(button)
        row.set_activatable_widget(button)
        return row

    def _on_mask_action(self, action: UnitAction) -> None:
        if action is UnitAction.UNMASK:
            self._run_action(action)
            return
        prompts.confirm(
            self,
            _("Mask {unit}?").format(unit=self.unit.short_name),
            _("Links the unit to /dev/null so it cannot be started."),
            _("_Mask"),
            on_confirm=lambda: self._run_action(UnitAction.MASK),
            destructive=True,
        )

    @staticmethod
    def _row(title: str, value: str, help_text: str = "", mono: bool = False) -> Adw.ActionRow:
        if mono:
            # Paths read better as the row caption than as a cramped suffix.
            row = Adw.ActionRow(
                title=title,
                subtitle=value,
                title_selectable=False,
                subtitle_selectable=True,
                tooltip_text=value,
            )
            if help_text:
                row.set_tooltip_text(f"{value}\n{help_text}")
            return row
        row = Adw.ActionRow(title=title, subtitle=help_text, title_selectable=False)
        row.add_suffix(
            widgets.label(
                value,
                "dim-label",
                wrap=True,
                wrap_mode=Pango.WrapMode.WORD_CHAR,
                selectable=True,
                xalign=1,
                width_chars=min(len(value), 12),  # short values like “4.5 MB” never break
                max_width_chars=48,
            )
        )
        return row

    @staticmethod
    def _recent_row(entry: LogEntry) -> Gtk.ListBoxRow:
        box = Gtk.Box(spacing=14, margin_top=10, margin_bottom=10, margin_start=16, margin_end=16)
        box.append(widgets.dot(words.level_dot(entry.priority), small=True))
        message = widgets.label(entry.message, hexpand=True, wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR)
        css = words.level(entry.priority)[1]
        if css:
            message.add_css_class(css)
        box.append(message)
        box.append(widgets.label(words.ago(entry.timestamp), "dim-label", "caption", valign=Gtk.Align.START))
        return Gtk.ListBoxRow(child=box, activatable=False)

    def _on_startup_toggled(self, row, _pspec):
        if row.get_active() == words.starts_at_boot(self.unit):
            return
        self._run_action(UnitAction.ENABLE if row.get_active() else UnitAction.DISABLE)

    # -- actions ----------------------------------------------------------

    def _edit_unit_file(self):
        details = self._details
        if not details:
            return
        content = unit_file_body(details.unit_file)
        if not content:
            return

        def on_saved(_name: str):
            self.manager.invalidate_unit_files()
            if not self._closed:
                self.toast_overlay.add_toast(Adw.Toast(title=_("Unit file saved"), timeout=3))
                self.load()
            self._on_changed()

        dialog = CreateUnitDialog(
            self.manager,
            self.scope,
            self._machine_label,
            self.operations,
            on_created=on_saved,
            edit_name=self.unit.name,
            edit_content=content,
        )
        dialog.present(self)

    def _edit_override(self):
        details = self._details
        if not details:
            return
        content = override_body(details.unit_file, self.unit.name) or words.override_template(self.unit)

        def on_saved(_name: str):
            if not self._closed:
                self.toast_overlay.add_toast(Adw.Toast(title=_("Override saved"), timeout=3))
                self.load()
            self._on_changed()

        dialog = CreateUnitDialog(
            self.manager,
            self.scope,
            self._machine_label,
            self.operations,
            on_created=on_saved,
            edit_name=self.unit.name,
            edit_content=content,
            override=True,
        )
        dialog.present(self)

    def _on_signal(self, signal: str):
        if signal != "SIGKILL":
            self._send_signal(signal)
            return
        prompts.confirm(
            self,
            _("Kill {unit}?").format(unit=self.unit.short_name),
            _("Its processes end at once, with no chance to save their work or clean up."),
            _("_Kill"),
            lambda: self._send_signal(signal),
            destructive=True,
        )

    def _send_signal(self, signal: str):
        manager, name, scope = self.manager, self.unit.name, self.scope
        self._run(
            lambda: manager.kill(name, signal, scope),
            _("Sent {signal} to {unit}").format(signal=signal, unit=self.unit.short_name),
            _("Could Not Send {signal}").format(signal=signal),
        )

    def _run_action(self, action: UnitAction, name: str = ""):
        """``action`` on this unit, or on the one named (a timer's service)."""
        manager, scope = self.manager, self.scope
        target = Unit(name=name, scope=scope) if name else self.unit

        def work():
            manager.control(target.name, action, scope)
            if action in (UnitAction.ENABLE, UnitAction.DISABLE, UnitAction.MASK, UnitAction.UNMASK):
                manager.invalidate_unit_files()

        self._run(work, self._action_message(target, action), _("Action Failed"))

    def _run(self, work: Callable[[], None], message: str, error_heading: str):
        self._set_busy(True)
        manager = self.manager

        def success(_result):
            if not self._closed:
                self.toast_overlay.add_toast(Adw.Toast(title=message, use_markup=False, timeout=3))
            self._on_changed()

        def finish():
            if not self._closed:
                self._set_busy(False)
                self.load()

        self.operations.run(
            manager,
            work,
            on_success=success,
            on_finish=finish,
            error_heading=error_heading,
        )


class UnitDialog(Adw.Dialog):
    """:class:`UnitPanel` in a dialog, for units opened outside the services list."""

    def __init__(self, *args, **kwargs):
        super().__init__(content_width=800, content_height=680, width_request=360, height_request=294)
        self.panel = UnitPanel(*args, **kwargs)
        self.set_child(self.panel)
        self.set_title(self.panel.unit.short_name)
        self.connect("closed", lambda *_: self.panel.discard())

    def start_loading(self) -> None:
        self.panel.start_loading()
