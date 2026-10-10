"""Details, logs and controls for one unit, as a side panel or a dialog."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from gettext import gettext as _

from gi.repository import Adw, GLib, GObject, Gtk, Pango

from ..core.manager import SystemdManager
from ..core.models import LogEntry, LogResult, Scope, Unit, UnitAction
from ..core.parsers import parse_int, unit_file_body
from ..core.ssh import SSHRunner
from . import text, widgets, words
from .create_unit_dialog import CreateUnitDialog
from .operations import Operations, describe
from .resources import template
from .tasks import run_in_thread

_SIMPLE_PAGES = ("overview", "activity")
_ADVANCED_PAGES = ("status", "logs", "file", "properties")
# The matching page when switching between simple and advanced.
_COUNTERPART = {"activity": "logs", "logs": "activity"}
_ACTIVITY_LIMIT = 300

_EXEC_PATH_RE = re.compile(r"path=(\S+)")


@dataclass
class _Details:
    unit: Unit
    status: str
    properties: dict[str, str]
    unit_file: str
    logs: LogResult


@template("unit-dialog.ui")
class UnitPanel(Adw.BreakpointBin):
    """Shown beside the services list, or inside :class:`UnitDialog`."""

    __gtype_name__ = "SystemdPilotUnitPanel"
    __gsignals__ = {"close-requested": (GObject.SignalFlags.RUN_FIRST, None, ())}

    toast_overlay: Adw.ToastOverlay = Gtk.Template.Child()
    header_bar: Adw.HeaderBar = Gtk.Template.Child()
    refresh_button: Gtk.Button = Gtk.Template.Child()
    close_button: Gtk.Button = Gtk.Template.Child()
    mode_box: Gtk.Box = Gtk.Template.Child()
    mode_switch: Gtk.Switch = Gtk.Template.Child()
    state_dot: Gtk.Box = Gtk.Template.Child()
    title_label: Gtk.Label = Gtk.Template.Child()
    name_label: Gtk.Label = Gtk.Template.Child()
    action_box: Gtk.FlowBox = Gtk.Template.Child()
    state_label: Gtk.Label = Gtk.Template.Child()
    stack: Adw.ViewStack = Gtk.Template.Child()
    overview_page: Adw.ViewStackPage = Gtk.Template.Child()
    activity_page: Adw.ViewStackPage = Gtk.Template.Child()
    status_page: Adw.ViewStackPage = Gtk.Template.Child()
    logs_page: Adw.ViewStackPage = Gtk.Template.Child()
    file_page: Adw.ViewStackPage = Gtk.Template.Child()
    properties_page: Adw.ViewStackPage = Gtk.Template.Child()
    overview_box: Gtk.Box = Gtk.Template.Child()
    activity_box: Gtk.Box = Gtk.Template.Child()
    status_view: Gtk.TextView = Gtk.Template.Child()
    logs_banner: Adw.Banner = Gtk.Template.Child()
    logs_view: Gtk.TextView = Gtk.Template.Child()
    logs_scroll: Gtk.ScrolledWindow = Gtk.Template.Child()
    file_view: Gtk.TextView = Gtk.Template.Child()
    edit_file_button: Gtk.Button = Gtk.Template.Child()
    props_view: Gtk.TextView = Gtk.Template.Child()
    props_search: Gtk.SearchEntry = Gtk.Template.Child()

    def __init__(
        self,
        manager: SystemdManager,
        unit: Unit,
        scope: Scope,
        operations: Operations,
        *,
        advanced: bool = False,
        machine_label: str = "",
        on_changed: Callable[[], None],
        action_message: Callable[[Unit, UnitAction], str],
        in_pane: bool = False,
    ):
        super().__init__()
        self.manager = manager
        self.unit = unit
        self.scope = scope
        self.operations = operations
        self._advanced = advanced
        self._machine_label = machine_label or _("This Computer")
        self._on_changed = on_changed
        self._action_message = action_message
        self._properties: dict[str, str] = {}
        self._details: _Details | None = None
        self._closed = False
        # On remote hosts, logs the SSH user may not read can be read through sudo.
        self._remote = isinstance(manager.runner, SSHRunner)
        self._elevated = False
        self._loads = 0
        self._details_load: int | None = None  # the load the shown details came from
        self.journal_badge: Gtk.Label | None = None  # on the window's Journal button, beside the list
        self.logs_banner.connect("button-clicked", lambda *_: self._view_logs_as_admin())

        # Beside the list the window's Simple/Advanced switch applies.
        self.mode_box.set_visible(not in_pane)
        self.close_button.set_visible(in_pane)
        if in_pane:
            # Too narrow to share a line with the name: actions go below the state line.
            row = self.action_box.get_parent()
            row.remove(self.action_box)
            row.get_parent().append(self.action_box)
            self.action_box.set_halign(Gtk.Align.START)

        self.mode_switch.set_active(self._advanced)
        self.mode_switch.connect("notify::active", self._on_switch)
        self._apply_mode(initial=True)

        self._show_unit(unit)
        self._build_shell()
        self._activity_loading()

    def start_loading(self) -> None:
        """Fetch fresh data in the background (call after :meth:`present`)."""
        self.load()

    @property
    def advanced(self) -> bool:
        return self._advanced

    def discard(self) -> None:
        """No longer shown: results still on their way are dropped."""
        self._closed = True

    def add_header_end(self, widget: Gtk.Widget) -> None:
        """A button of the window's, placed before the close button."""
        self.header_bar.pack_end(widget)

    def set_advanced(self, advanced: bool) -> None:
        if advanced != self._advanced:
            self._advanced = advanced
            self._apply_mode()

    def _on_switch(self, switch, _pspec):
        self.set_advanced(switch.get_active())

    def _apply_mode(self, initial: bool = False):
        advanced = self.advanced
        if self.mode_switch.get_active() != advanced:
            self.mode_switch.set_active(advanced)
        current = self.stack.get_visible_child_name()
        for name in _SIMPLE_PAGES:
            getattr(self, f"{name}_page").set_visible(not advanced)
        for name in _ADVANCED_PAGES:
            getattr(self, f"{name}_page").set_visible(advanced)
        if initial or current not in (_ADVANCED_PAGES if advanced else _SIMPLE_PAGES):
            fallback = "status" if advanced else "overview"
            self.stack.set_visible_child_name(_COUNTERPART.get(current, fallback) if not initial else fallback)
        self._show_actions(self.unit)

    # -- header -----------------------------------------------------------

    def _show_unit(self, unit: Unit):
        self.unit = unit
        self.title_label.set_label(unit.short_name)
        self.title_label.set_tooltip_text(unit.name)
        description = words.unit_description(unit)
        self.name_label.set_label(description)
        self.name_label.set_visible(bool(description))
        self.name_label.set_tooltip_text(description or None)
        widgets.set_dot(self.state_dot, unit.kind)
        self.state_dot.add_css_class("large")
        word = GLib.markup_escape_text(words.state_word(unit))
        sentence = GLib.markup_escape_text(words.state_sentence(unit))
        self.state_label.set_markup(f'<span weight="bold">{word}</span> · {sentence}')
        self.state_label.set_css_classes(["state-line"])
        self._show_actions(unit)

    def _show_actions(self, unit: Unit):
        widgets.clear(self.action_box)
        if unit.kind == "running":
            buttons = [
                (UnitAction.RESTART, _("_Restart"), "view-refresh-symbolic", None),
                (UnitAction.STOP, _("S_top"), "media-playback-stop-symbolic", "destructive-action"),
            ]
        else:
            label = _("_Try Again") if unit.is_failed else _("_Start")
            buttons = [(UnitAction.START, label, "media-playback-start-symbolic", "suggested-action")]
        if self.advanced and words.can_toggle_startup(unit):
            if words.starts_at_boot(unit):
                buttons.append((UnitAction.DISABLE, _("_Disable"), "window-close-symbolic", None))
            else:
                buttons.append((UnitAction.ENABLE, _("_Enable"), "emblem-ok-symbolic", None))
        for action, label, icon, css in buttons:
            button = Gtk.Button(child=Adw.ButtonContent(label=label, icon_name=icon, use_underline=True))
            if css:
                button.add_css_class(css)
            button.connect("clicked", lambda _b, a=action: self._run_action(a))
            self.action_box.append(button)

    def _set_busy(self, busy: bool):
        self.refresh_button.set_sensitive(not busy)
        self.action_box.set_sensitive(not busy)
        self.overview_box.set_sensitive(not busy)

    def _set_fetching(self, fetching: bool) -> None:
        """Background refresh: keep start/stop/enable usable while details load."""
        self.refresh_button.set_sensitive(not fetching)

    # -- loading ----------------------------------------------------------

    def load(self):
        manager, name, scope = self.manager, self.unit.name, self.scope
        self._loads += 1
        load = self._loads
        self._set_fetching(True)

        def fetch_core() -> tuple[dict[str, str], Unit]:
            props = manager.properties(name, scope)
            fresh = Unit(
                name=name,
                description=props.get("Description", self.unit.description),
                load_state=props.get("LoadState", ""),
                active_state=props.get("ActiveState", ""),
                sub_state=props.get("SubState", ""),
                file_state=props.get("UnitFileState", ""),
            )
            return props, manager.add_runtime([fresh], scope)[0]

        def core_done(result: tuple[dict[str, str], Unit]):
            if self._closed or load != self._loads:
                return
            props, unit = result
            self._properties = props
            self._show_unit(unit)
            self._build_overview(
                _Details(unit=unit, status="", properties=props, unit_file="", logs=LogResult([], ""))
            )
            self._load_heavy(props, unit, load)

        def core_failed(error):
            if self._closed or load != self._loads:
                return
            self._set_fetching(False)
            self._on_load_failed(error)

        if self._elevated:
            self._load_heavy_elevated(load)
            return

        run_in_thread(fetch_core, on_done=core_done, on_error=core_failed)

    def _load_heavy(self, props: dict[str, str], unit: Unit, load: int) -> None:
        manager, name, scope, elevated = self.manager, self.unit.name, self.scope, self._elevated

        def fetch_heavy() -> tuple[str, str, LogResult]:
            return (
                manager.status_text(name, scope),
                manager.unit_file(name, scope),
                manager.logs(name, scope, privileged=elevated),
            )

        def heavy_done(result: tuple[str, str, LogResult]):
            if self._closed or load != self._loads:
                return
            status, unit_file, logs = result
            self._on_loaded(
                _Details(unit=unit, status=status, properties=props, unit_file=unit_file, logs=logs),
                load,
            )

        def heavy_failed(error):
            if self._closed or load != self._loads:
                return
            self._set_fetching(False)
            self._on_load_failed(error)

        run_in_thread(fetch_heavy, on_done=heavy_done, on_error=heavy_failed)

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
            )
            unit = manager.add_runtime([unit], scope)[0]
            return _Details(
                unit=unit,
                status=manager.status_text(name, scope),
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
        self._set_fetching(False)
        self._show_unit(details.unit)
        self.status_view.get_buffer().set_text(details.status)
        self.file_view.get_buffer().set_text(details.unit_file or _("No unit file found."))
        self.edit_file_button.set_sensitive(bool(unit_file_body(details.unit_file)))
        self._properties = details.properties
        text.set_properties(self.props_view.get_buffer(), self._properties, self.props_search.get_text())
        text.set_logs(self.logs_view.get_buffer(), details.logs.entries)
        if not details.logs.entries:
            self.logs_view.get_buffer().set_text(_("No log entries."))
        self.logs_banner.set_title(GLib.markup_escape_text(self._logs_warning(details)))
        self.logs_banner.set_button_label(_("View as Administrator") if self._remote else None)
        self.logs_banner.set_revealed(bool(details.logs.warning))
        GLib.idle_add(self._scroll_logs_to_end)
        self._build_overview(details)
        GLib.idle_add(self._build_activity_idle, details)

    def _build_activity_idle(self, details: _Details):
        if not self._closed:
            self._build_activity(details)
        return GLib.SOURCE_REMOVE

    def _scroll_logs_to_end(self):
        adj = self.logs_scroll.get_vadjustment()
        adj.set_value(adj.get_upper() - adj.get_page_size())
        return GLib.SOURCE_REMOVE

    def _on_load_failed(self, error):
        if self._closed:
            return
        self._set_fetching(False)
        message = describe(error)
        self.status_view.get_buffer().set_text(message)
        widgets.clear(self.overview_box)
        self.overview_box.append(
            Adw.StatusPage(
                icon_name="dialog-warning-symbolic",
                title=_("Could Not Load Details"),
                description=GLib.markup_escape_text(message),
            )
        )
        self.toast_overlay.add_toast(Adw.Toast(title=_("Could not load details"), timeout=3))

    def _build_shell(self) -> None:
        """Overview from the list row so the dialog can open immediately."""
        unit = self.unit
        box = self.overview_box
        widgets.clear(box)
        if unit.is_failed:
            banner = Gtk.Box(spacing=14, css_classes=["error-banner"])
            banner.append(Gtk.Image(icon_name="dialog-error-symbolic", css_classes=["error"]))
            banner.append(widgets.label(_("This service stopped with an error"), "heading", "error"))
            box.append(banner)
        behavior = Adw.PreferencesGroup(title=_("Behavior"))
        behavior.add(
            self._row(_("Enabled"), _("Loading…"), _("Checking whether this starts at boot"))
        )
        box.append(behavior)

    def _activity_loading(self) -> None:
        widgets.clear(self.activity_box)
        self.activity_box.append(
            Gtk.Spinner(spinning=True, width_request=32, height_request=32, margin_top=48, halign=Gtk.Align.CENTER)
        )

    # -- simple pages -----------------------------------------------------

    def _build_overview(self, details: _Details):
        unit, props = details.unit, details.properties
        box = self.overview_box
        widgets.clear(box)

        if unit.is_failed and details.logs.entries:
            box.append(self._error_banner(details))
        elif unit.is_failed:
            banner = Gtk.Box(spacing=14, css_classes=["error-banner"])
            banner.append(Gtk.Image(icon_name="dialog-error-symbolic", css_classes=["error"]))
            banner.append(widgets.label(_("This service stopped with an error"), "heading", "error"))
            box.append(banner)

        behavior = Adw.PreferencesGroup(title=_("Behavior"))
        if unit.kind == "running" and unit.main_pid:
            program = (props.get("ExecMainPath") or self._program(props)).rsplit("/", 1)[-1] or unit.short_name
            behavior.add(
                self._row(_("Main process"), f"{program} (#{unit.main_pid})", _("The program this service runs"))
            )
        if words.can_toggle_startup(unit):
            enabled = words.starts_at_boot(unit)
            row = Adw.SwitchRow(
                title=_("Enabled"),
                subtitle=_("Starts every time the computer boots") if enabled else _("Only runs when you start it"),
                active=enabled,
            )
            row.connect("notify::active", self._on_startup_toggled)
            behavior.add(row)
        else:
            help_text = (
                _("Other services start this one when they need it")
                if unit.file_state in ("static", "indirect")
                else _("Can’t be turned on or off at boot")
            )
            behavior.add(self._row(_("Enabled"), words.boot_text(unit.file_state), help_text))
        box.append(behavior)

        if unit.kind == "running":
            resources = Adw.PreferencesGroup(title=_("Resources"))
            memory = parse_int(props.get("MemoryCurrent", ""))
            peak = parse_int(props.get("MemoryPeak", ""))
            resources.add(
                self._row(
                    _("Memory"),
                    words.size(memory),
                    _("Highest so far: {size}").format(size=words.size(peak)) if peak else "",
                )
            )
            resources.add(
                self._row(
                    _("Processor time"),
                    words.cpu_time(parse_int(props.get("CPUUsageNSec", ""))),
                    _("Total since it started"),
                )
            )
            tasks = parse_int(props.get("TasksCurrent", ""))
            limit = parse_int(props.get("TasksMax", ""))
            resources.add(
                self._row(
                    _("Tasks"),
                    str(tasks) if tasks is not None else "—",
                    _("Threads and child processes, limit {n}").format(n=f"{limit:,}")
                    if limit
                    else _("Threads and child processes"),
                )
            )
            box.append(resources)

        where = Adw.PreferencesGroup(title=_("Where it lives"))
        path = props.get("FragmentPath") or ""
        file_row = self._row(_("Configuration file"), path or _("None"), mono=True)
        if path and unit_file_body(details.unit_file):
            edit = Gtk.Button(
                label=_("_Edit"),
                use_underline=True,
                valign=Gtk.Align.CENTER,
                css_classes=["small-pill"],
            )
            edit.connect("clicked", lambda *_: self._edit_unit_file())
            file_row.add_suffix(edit)
        where.add(file_row)
        program = self._program(props)
        if program:
            where.add(self._row(_("Program"), program, mono=True))
        box.append(where)

        if details.logs.entries or details.status:
            recent = [e for e in reversed(details.logs.entries[-3:])]
            activity = Adw.PreferencesGroup(title=_("Recent activity"))
            more = Gtk.Button(label=_("All Activity"), css_classes=["flat"], valign=Gtk.Align.CENTER)
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
        texts.append(widgets.label(_("This service stopped with an error"), "heading", "error"))
        errors = [e for e in details.logs.entries if e.priority <= 3]
        reason = errors[-1].message if errors else details.properties.get("Result", "")
        if reason:
            texts.append(widgets.label(reason, "issue-explanation", wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR))
        banner.append(texts)
        button = Gtk.Button(label=_("See What Happened"), valign=Gtk.Align.START, css_classes=["small-pill"])
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
        entries = list(reversed(details.logs.entries))[:_ACTIVITY_LIMIT]
        section = widgets.Section(_("Activity"), _("Newest first"))
        for entry in entries:
            section.list.append(widgets.log_row(entry, show_source=False))
        if not entries:
            section.list.append(widgets.placeholder_row(_("No log entries.")))
        box.append(section.box)

    def _logs_warning(self, details: _Details) -> str:
        if self._remote:
            return _("Some entries may be hidden: only entries this user may read are shown.")
        return details.logs.warning

    @staticmethod
    def _program(props: dict[str, str]) -> str:
        match = _EXEC_PATH_RE.search(props.get("ExecStart", ""))
        return match.group(1) if match else ""

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

    @Gtk.Template.Callback()
    def on_refresh_clicked(self, _button):
        self.load()

    @Gtk.Template.Callback()
    def on_close_clicked(self, _button):
        self.emit("close-requested")

    @Gtk.Template.Callback()
    def on_edit_file_clicked(self, _button):
        self._edit_unit_file()

    @Gtk.Template.Callback()
    def on_props_search_changed(self, entry):
        text.set_properties(self.props_view.get_buffer(), self._properties, entry.get_text())

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
