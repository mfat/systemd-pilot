"""The journal page: problems found in the system log, then the log itself."""

from __future__ import annotations

from collections.abc import Callable

from gi.repository import Adw, Gio, GLib, GObject, Graphene, Gtk

from ..core.journal import ERROR, PRESETS, PRESETS_BY_ID, WARNING, Issue, find_issues
from ..core.manager import ACCESS_MISSING, ACCESS_PENDING, SystemdManager
from ..core.models import LogEntry, LogResult
from ..core.ssh import SSHRunner
from ..i18n import _, ngettext
from . import prompts, widgets, words
from .operations import Operations, describe
from .tasks import run_in_thread
from .widgets import FilterRow, Option, OptionButton

LIMIT = 1500  # newest entries fetched; enough to spot problems without a slow transfer over SSH
PAGE = 300  # rows shown in the simple timeline before “Show More”
CHUNK = 40  # timeline rows created per idle tick so the first open stays responsive

# value, button text, phrase for the subtitle, journalctl --since
SINCE = (
    ("1h", _("Last hour"), _("in the last hour"), "1 hour ago"),
    ("24h", _("Last 24 hours"), _("in the last 24 hours"), "24 hours ago"),
    ("today", _("Today"), _("today"), "today"),
    ("7d", _("Last 7 days"), _("in the last 7 days"), "7 days ago"),
    ("any", _("Any time"), "", ""),
)
# value, option label, help, button text, --boot
BOOTS = (
    ("all", _("All boots"), _("Don’t limit by startup"), _("all boots"), None),
    ("0", _("This boot"), _("Since the computer last started"), _("this boot"), 0),
    ("-1", _("Previous boot"), _("The time before the last restart"), _("the previous boot"), -1),
    ("-2", _("Two boots ago"), _("The time before that"), _("two boots ago"), -2),
)
# value, option label, help, button text, kernel only
SOURCES = (
    ("all", _("Everything"), _("Services, programs and the kernel"), _("all sources"), False),
    ("kernel", _("Kernel only"), _("Hardware, drivers and memory messages"), _("the kernel only"), True),
)
# value, sidebar label, dot (or icon), timeline title
FILTERS = (
    ("problems", _("Flagged"), "dialog-warning-symbolic", _("Flagged entries")),
    ("errors", _("Errors"), "failed", _("Errors")),
    ("warnings", _("Warnings"), "warning", _("Warnings")),
    ("all", _("All entries"), "bell-symbolic", _("All entries")),
)
# Chips that keep only the problem cards of one severity, and what to say when there are none.
CARD_FILTERS = {
    "errors": (ERROR, _("No errors found in this range")),
    "warnings": (WARNING, _("No warnings found in this range")),
}
PRESET_GROUPS = (
    ("stability", _("System stability & crashes")),
    ("security", _("Security, auth & privileges")),
    ("desktop", _("Desktop & session")),
    ("network", _("Networking & peripherals")),
    ("lifecycle", _("Lifecycle & startup")),
)
PRESET_TEXT = {
    "coredump": (_("Core Dumps & Segfaults"), _("Crashed programs, aborted processes, stack traces")),
    "oom": (_("Out of Memory"), _("Kernel OOM kills, cgroup memory exhaustion")),
    "storage": (_("Storage & Drive Errors"), _("Failing sectors, read-only remounts, SMART warnings")),
    "sudo": (_("Sudo & Privileges"), _("Failed password prompts, escalation attempts")),
    "ssh": (_("SSH & Remote Access"), _("Accepted and rejected logins, brute-force attempts")),
    "mac": (_("SELinux & AppArmor Denials"), _("Blocked by security confinement, not file permissions")),
    "gpu": (_("Compositor & GPU Resets"), _("Display freezes, GPU lockups, page-flip timeouts")),
    "audio": (_("Audio Glitches"), _("PipeWire and ALSA dropouts, buffer underruns")),
    "flatpak": (_("Flatpak & Portals"), _("Portal denials, missing D-Bus interfaces")),
    "network": (_("Network & DNS"), _("DHCP timeouts, DNS drops, Wi-Fi association failures")),
    "usb": (_("USB & Peripherals"), _("Disconnects, resets, dock and hub problems")),
    "boot": (_("Boot Problems"), _("Services that failed or timed out during this boot")),
    "packages": (_("Package Updates"), _("Installs, upgrades and failed transactions")),
}

_FILTER_MATCH: dict[str, Callable[[int, LogEntry, dict], bool]] = {
    "problems": lambda i, _e, flagged: i in flagged,
    "errors": lambda _i, e, _f: e.priority <= 3,
    "warnings": lambda _i, e, _f: e.priority == 4,
    "all": lambda _i, _e, _f: True,
}


def _pick(table, value):
    return next((row for row in table if row[0] == value), table[0])


def issue_text(issue: Issue) -> tuple[str, str]:
    """(title, explanation) for a problem."""
    names = ", ".join(issue.names[:3])
    count = len(issue.entries)
    if issue.kind == "crash":
        title = ngettext("A program crashed", "Programs crashed", max(1, len(issue.names)))
        explanation = (
            _("{names} stopped unexpectedly. A crash report may have been saved.").format(names=names)
            if names
            else _("A program stopped unexpectedly.")
        )
    elif issue.kind == "oom":
        title = _("The system ran out of memory")
        explanation = (
            _("Memory ran low, so {names} was stopped to recover. Unsaved work in it may be lost.").format(names=names)
            if names
            else _("Memory ran low, so a program was stopped to recover. Unsaved work in it may be lost.")
        )
    elif issue.kind == "ssh":
        title = _("Failed login attempts")
        explanation = ngettext(
            "{n} failed SSH login from {names}.", "{n} failed SSH logins from {names}.", count
        ).format(n=count, names=names or _("unknown addresses"))
        explanation += " " + _("If you don’t recognise these addresses, block them or turn off password logins.")
    elif issue.kind == "storage":
        title = _("Storage errors")
        explanation = _("The kernel reported disk errors. Back up important data and check the drive’s health.")
    elif issue.kind == "unit-failed":
        name = issue.unit.removesuffix(".service")
        failures = sum(1 for e in issue.entries if "Failed with result" in e.message)
        title = _("{unit} keeps failing").format(unit=name) if failures > 1 else _("{unit} failed").format(unit=name)
        explanation = (
            _("The service stopped with an error ({result}).").format(result=", ".join(issue.names))
            if issue.names
            else _("The service stopped with an error.")
        )
    elif issue.kind == "errors":
        title = _("{source} reported errors").format(source=issue.source.removesuffix(".service"))
        explanation = issue.latest.message
    else:
        title = _("Repeated warnings from {source}").format(source=issue.source.removesuffix(".service"))
        explanation = issue.latest.message
    return title, explanation


def issue_badge(issue: Issue) -> str:
    """The problem in a word or two, without the names, for the badge on its entries."""
    if issue.kind == "unit-failed":
        failures = sum(1 for e in issue.entries if "Failed with result" in e.message)
        return _("Keeps failing") if failures > 1 else _("Failed")
    badges = {
        "crash": _("Crashed"),
        "oom": _("Out of memory"),
        "ssh": _("Failed login"),
        "storage": _("Storage error"),
        "errors": _("Errors"),
    }
    return badges.get(issue.kind, _("Repeated warnings"))


class IssueRow(Gtk.ListBoxRow):
    """A problem card. Selecting it shows its entries, and its service, in the details column."""

    def __init__(self, issue: Issue):
        super().__init__()
        self.issue = issue
        title, explanation = issue_text(issue)
        error = issue.severity == ERROR
        box = Gtk.Box(spacing=14, margin_top=14, margin_bottom=14, margin_start=16, margin_end=16)
        icon = "dialog-error-symbolic" if error else "dialog-warning-symbolic"
        box.append(Gtk.Image(icon_name=icon, valign=Gtk.Align.START, css_classes=["error" if error else "warning"]))

        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3, hexpand=True)
        heading = Gtk.Box(spacing=8)
        heading.append(widgets.label(title, "unit-title", wrap=True, hexpand=True))
        badge = Gtk.Label(label=_("Error") if error else _("Warning"), valign=Gtk.Align.CENTER)
        badge.set_css_classes(["badge", "error" if error else "warning"])
        heading.append(badge)
        texts.append(heading)
        if explanation:
            explanation = explanation.partition("\n")[0]
            texts.append(widgets.label(explanation, "issue-explanation", wrap=True))
        texts.append(widgets.label(issue_meta(issue), "dim-label", "caption", wrap=True))
        box.append(texts)

        box.append(Gtk.Image(icon_name="go-next-symbolic", css_classes=["dim-label"], valign=Gtk.Align.CENTER))
        self.set_child(box)
        self.update_property([Gtk.AccessibleProperty.LABEL], [title])


def issue_meta(issue: Issue) -> str:
    """Where a problem comes from, how often it was seen and when last."""
    count = len(issue.entries)
    return " · ".join(
        (
            issue.unit or issue.source,
            ngettext("{n} entry", "{n} entries", count).format(n=count),
            _("last seen {ago}").format(ago=words.ago(issue.latest.timestamp)) if issue.latest.timestamp else "",
        )
    ).strip(" ·")


class JournalView(Gtk.Box):
    """The problems and the timeline, for the middle column; :attr:`sidebar` holds their filters.

    Emits ``open-unit`` (unit name), ``changed`` when the summary or problem count
    changes, ``selected`` (an :class:`Issue` or :class:`LogEntry`, or None when it
    is no longer listed) and ``activated`` (the same) when a row is clicked.
    """

    __gtype_name__ = "SystemdPilotJournalView"
    __gsignals__ = {
        "open-unit": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "selected": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        "activated": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
    }

    def __init__(self, operations: Operations):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.operations = operations
        self._manager: SystemdManager | None = None
        self._access = ""  # ACCESS_* when entries are hidden, else ""
        self._elevated = False  # read as root through sudo (remote hosts)
        self._banner_action: Callable[[], None] | None = None
        self._generation = 0
        self._result: LogResult | None = None
        self._boot_id = ""
        self._query = ""
        self._known_units: set[str] = set()
        self._shown = PAGE
        self.issues: list[Issue] = []
        self.entry_count = 0
        self.loaded = False
        self._loading = False
        self._stale = False
        self._needs_render = True  # False after a UI render matches the current analysis
        self._render_gen = 0  # cancels in-flight chunked timeline paints
        self._jump_to_timeline = False  # a filter was chosen: bring its entries into view
        self.selected: Issue | LogEntry | None = None  # shown in the details column
        # Cached filter work: recomputed when the entry set, query or preset changes.
        self._analysis_key: tuple | None = None
        self._entries: list[LogEntry] = []
        self._ranged: list[LogEntry] = []
        self._flagged: dict = {}
        self._filter_counts: dict[str, int] = {}
        self._counts_ready = False

        actions = Gio.SimpleActionGroup()
        defaults = {"filter": "problems", "since": "24h", "boot": "all", "source": "all", "preset": ""}
        for name, default in defaults.items():
            action = Gio.SimpleAction.new_stateful(name, GLib.VariantType.new("s"), GLib.Variant("s", default))
            action.connect("change-state", self._on_option, name)
            actions.add_action(action)
        self._actions = actions
        self.insert_action_group("journal", actions)

        # The filters, presets and range live in the window sidebar.
        self.sidebar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin_bottom=12)
        self.sidebar.insert_action_group("journal", actions)
        self.filter_list = Gtk.ListBox(css_classes=["navigation-sidebar"])
        self._filter_rows: dict[str, FilterRow] = {}
        for value, label, mark, _title in FILTERS:
            icon = mark if mark.endswith("-symbolic") else ""
            row = FilterRow(value, label, None if icon else mark, icon)
            self._filter_rows[value] = row
            self.filter_list.append(row)
        self.filter_list.select_row(self._filter_rows["problems"])
        self.filter_list.connect("row-selected", self._on_filter_row_selected)
        self.sidebar.append(self.filter_list)

        groups = []
        for group, heading in PRESET_GROUPS:
            options = [Option(p.id, *PRESET_TEXT[p.id]) for p in PRESETS if p.group == group]
            groups.append((heading, options))
        self._presets = OptionButton(
            "journal.preset",
            groups,
            intro=_("Ready-made searches for common problems. Counts use the range below."),
            width=420,
            counts=True,
        )
        self._presets.set_hexpand(True)
        self._presets.set_text(_("No preset"))
        self._clear_preset = Gtk.Button(
            icon_name="window-close-symbolic",
            tooltip_text=_("Remove Preset"),
            visible=False,
            css_classes=["chip", "preset-active", "preset-clear"],
            action_name="journal.preset",
            action_target=GLib.Variant("s", ""),
        )
        preset_box = Gtk.Box(css_classes=["linked"])
        preset_box.append(self._presets)
        preset_box.append(self._clear_preset)
        self.sidebar.append(self._sidebar_group(_("Presets"), preset_box))

        # The range the entries come from.
        self._since = OptionButton(
            "journal.since",
            [(None, [Option(v, label, "", f"--since “{flag}”" if flag else "") for v, label, _p, flag in SINCE])],
            css=("picker",),
        )
        self._boot = OptionButton(
            "journal.boot",
            [
                (
                    None,
                    [
                        Option(v, label, help, f"-b {b}" if b else ("-b" if b == 0 else ""))
                        for v, label, help, _t, b in BOOTS
                    ],
                )
            ],
            css=("picker",),
        )
        self._source = OptionButton(
            "journal.source",
            [(None, [Option(v, label, help, "-k" if kernel else "") for v, label, help, _t, kernel in SOURCES])],
            css=("picker",),
        )
        pickers = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        for button in (self._since, self._boot, self._source):
            button.set_hexpand(True)
            pickers.append(button)
        self.sidebar.append(self._sidebar_group(_("Range"), pickers))

        self._banner = Adw.Banner()
        self._banner.connect("button-clicked", lambda *_: self._banner_action and self._banner_action())
        self.append(self._banner)

        self.stack = Gtk.Stack(vexpand=True, hhomogeneous=False, transition_type=Gtk.StackTransitionType.CROSSFADE)
        spinner = Gtk.Spinner(
            spinning=True, width_request=32, height_request=32, halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER
        )
        self.stack.add_named(spinner, "loading")

        self._simple = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=22,
            margin_top=8,
            margin_bottom=24,
            margin_start=24,
            margin_end=24,
            hexpand=True,
        )
        self._simple_scroll = Gtk.ScrolledWindow(child=self._simple, hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.stack.add_named(self._simple_scroll, "simple")

        self._status = Adw.StatusPage()
        self.stack.add_named(self._status, "status")
        self.append(self.stack)

        self._update_pickers()

    # -- public API -------------------------------------------------------

    def set_manager(self, manager: SystemdManager | None) -> None:
        """Show another machine's journal; it is loaded by :meth:`reload`."""
        if manager is self._manager:
            return
        self._manager = manager
        self._generation += 1
        self._result = None
        self._boot_id = ""
        self._access = ""
        self._elevated = False
        self.issues = []
        self.entry_count = 0
        self.loaded = False
        self._loading = False
        self._stale = False
        self._analysis_key = None
        self._needs_render = True
        self._render_gen += 1
        self._select(None)
        self.emit("changed")

    def mark_stale(self) -> None:
        """Note that a later visit should refetch; do not refetch in the background."""
        if self.loaded:
            self._stale = True

    def ensure_loaded(self) -> None:
        """Fetch in the background for the issues badge, without building the page widgets."""
        if not self.loaded and not self._loading:
            self.reload(ui=False)

    def show(self) -> None:
        """Open the journal page: fetch if needed, or refresh if services changed it."""
        if self._stale or not self.loaded:
            self.reload(ui=True)
        elif self._needs_render:
            self._refresh()

    def reload(self, *, ui: bool = True) -> None:
        manager = self._manager
        if manager is None:
            return
        self._generation += 1
        generation = self._generation
        self._loading = True
        self._stale = False
        if ui and self._result is None:
            self.stack.set_visible_child_name("loading")
        since = _pick(SINCE, self._state("since"))[3]
        boot = _pick(BOOTS, self._state("boot"))[4]
        kernel = _pick(SOURCES, self._state("source"))[4]
        need_boot_id = not self._boot_id
        elevated = self._elevated

        def fetch():
            boot_id = manager.boot_id() if need_boot_id else None
            result = manager.journal(since=since, boot=boot, kernel=kernel, lines=LIMIT, privileged=elevated)
            # Hidden entries: say why, and whether the user can do something about it.
            access = manager.journal_access() if result.warning else ""
            return result, boot_id, access

        def done(result):
            if generation != self._generation:
                return
            self._loading = False
            self._result, boot_id, self._access = result
            if boot_id is not None:
                self._boot_id = boot_id
            self.loaded = True
            self._stale = False
            self._shown = PAGE
            self._analysis_key = None
            self._refresh(ui=ui)

        def failed(error):
            if generation != self._generation:
                return
            self._loading = False
            self._result = None
            self._analysis_key = None
            self.issues = []
            if ui:
                self._status.set_icon_name("dialog-warning-symbolic")
                self._status.set_title(_("Could Not Read the Journal"))
                self._status.set_description(GLib.markup_escape_text(describe(error)))
                self.stack.set_visible_child_name("status")
            self.emit("changed")

        if not elevated:
            run_in_thread(fetch, on_done=done, on_error=failed)
            return

        def finished():
            # Cancelled or failed (the error was shown): go back to what the user can read.
            if generation == self._generation and self._loading:
                self._elevated = False
                self.reload(ui=ui)

        # Asks for the sudo password when needed, once per connection.
        self.operations.run(
            manager,
            fetch,
            on_success=done,
            on_finish=finished,
            error_heading=_("Could Not Read the Journal as Administrator"),
        )

    def set_query(self, query: str) -> None:
        self._query = query.strip().lower()
        self._shown = PAGE
        self._refresh()

    def set_known_units(self, names: set[str]) -> None:
        self._known_units = names

    def knows_unit(self, name: str) -> bool:
        return name in self._known_units

    @property
    def boot_id(self) -> str:
        return self._boot_id

    # -- internals --------------------------------------------------------

    def _sidebar_group(self, heading: str, child: Gtk.Widget) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin_top=12, margin_start=12, margin_end=12)
        box.append(widgets.label(heading, "dim-label", "caption-heading", margin_start=6))
        box.append(child)
        return box

    def _on_filter_row_selected(self, _listbox, row):
        if row is not None and row.value != self._state("filter"):
            self._actions.activate_action("filter", GLib.Variant("s", row.value))

    def _select(self, item: Issue | LogEntry | None) -> None:
        if item is not self.selected:
            self.selected = item
            self.emit("selected", item)

    def _on_row_selected(self, listbox, row, other: Gtk.ListBox):
        # One selection across the problems and the timeline.
        if row is None:
            return
        if other is not None:
            other.unselect_all()
        self._select(getattr(row, "issue", None) or getattr(row, "entry", None))

    def _on_row_activated(self, _listbox, row):
        item = getattr(row, "issue", None) or getattr(row, "entry", None)
        if item is not None:
            self.emit("activated", item)

    def _state(self, name: str) -> str:
        return self._actions.lookup_action(name).get_state().get_string()

    def _on_option(self, action, value, name):
        if name == "preset" and value.get_string() == action.get_state().get_string():
            value = GLib.Variant("s", "")  # choosing the active preset again removes it
        action.set_state(value)
        if name == "preset" and value.get_string():
            self._actions.lookup_action("filter").set_state(GLib.Variant("s", "all"))
        self.filter_list.select_row(self._filter_rows[self._state("filter")])
        self._update_pickers()
        if name in ("since", "boot", "source"):
            self.reload()
        else:
            # The filters change the timeline below the problems; without this, a click changes nothing in view.
            self._jump_to_timeline = name == "filter"
            self._shown = PAGE
            self._refresh()

    def _update_pickers(self) -> None:
        self._since.set_text(_pick(SINCE, self._state("since"))[1])
        self._boot.set_text(_pick(BOOTS, self._state("boot"))[1])
        self._source.set_text(_pick(SOURCES, self._state("source"))[1])

    def _refresh(self, *, ui: bool = True) -> None:
        if self._result is None:
            return
        query = self._query
        preset_id = self._state("preset")
        preset = PRESETS_BY_ID.get(preset_id)
        # Searching, changing preset, or a new fetch: re-scan. Filter changes reuse this.
        analysis_key = (id(self._result), query, preset_id, self._boot_id)
        if analysis_key != self._analysis_key:
            self._analysis_key = analysis_key
            ranged = [
                e
                for e in self._result.entries
                if not query or query in e.message.lower() or query in e.identifier.lower()
            ]
            entries = [e for e in ranged if preset.matches(e, self._boot_id)] if preset else ranged
            self.issues, flagged = find_issues(entries)
            self._ranged = ranged
            self._entries = entries
            self._flagged = flagged
            self.entry_count = len(entries)
            self._counts_ready = False
            self._needs_render = True

        if not ui:
            # Badge path: issues only — skip chip/preset counts and widget builds.
            self.emit("changed")
            return

        if not self._counts_ready:
            flagged = self._flagged
            self._filter_counts = {
                value: sum(1 for i, e in enumerate(self._entries) if match(i, e, flagged))
                for value, match in _FILTER_MATCH.items()
            }
            preset_counts = dict.fromkeys((p.id for p in PRESETS), 0)
            for entry in self._ranged:
                for p in PRESETS:
                    if p.matches(entry, self._boot_id):
                        preset_counts[p.id] += 1
            for p in PRESETS:
                self._presets.set_option_count(p.id, preset_counts[p.id])
            self._counts_ready = True

        entries, flagged = self._entries, self._flagged
        for value, row in self._filter_rows.items():
            row.set_count(self._filter_counts.get(value, 0))
        if preset:
            self._presets.set_text(f"{PRESET_TEXT[preset.id][0]}  {len(entries)}")
            self._presets.set_css_classes(["chip", "preset-active"])
        else:
            self._presets.set_text(_("No preset"))
            self._presets.set_css_classes(["chip"])
        self._clear_preset.set_visible(preset is not None)

        self._update_banner()

        flt = self._state("filter")
        shown = [(i, e) for i, e in enumerate(entries) if _FILTER_MATCH[flt](i, e, flagged)]
        self._render_simple(shown, flagged, flt)
        self._needs_render = False
        self.emit("changed")

    # -- hidden entries ---------------------------------------------------

    def _update_banner(self) -> None:
        warning, button, action = "", "", None
        remote = isinstance(self._manager.runner, SSHRunner) if self._manager else False
        if self._result.warning and remote:
            warning = _("Only entries this user may read are shown.")
            button, action = _("View as Administrator"), self._view_as_admin
        elif self._result.warning and self._access == ACCESS_PENDING:
            warning = _("Log out and back in to see all entries.")
        elif self._result.warning and self._access == ACCESS_MISSING:
            warning = _("You’re only seeing your own entries.")
            button, action = _("Allow Access…"), self._ask_grant_access
        elif self._result.warning:
            warning = self._result.warning
        elif len(self._result.entries) >= LIMIT:
            warning = _("Only the newest {n} entries are shown. Choose a shorter range to see older ones.").format(
                n=LIMIT
            )
        self._banner_action = action
        self._banner.set_title(GLib.markup_escape_text(warning))
        self._banner.set_button_label(button or None)
        self._banner.set_revealed(bool(warning))

    def _view_as_admin(self) -> None:
        self._elevated = True
        self.reload()

    def _ask_grant_access(self) -> None:
        prompts.confirm(
            self,
            _("Allow Access to All Logs?"),
            _(
                "Your account will be added to the “systemd-journal” group, so you can read the logs of the "
                "whole system without a password. Logs can include other users’ activity. "
                "This applies after you log out and back in."
            ),
            _("_Allow Access"),
            self._grant_access,
        )

    def _grant_access(self) -> None:
        manager = self._manager
        if manager is None:
            return

        def granted(_result):
            if manager is self._manager:
                self._access = ACCESS_PENDING
                self._update_banner()
            self.operations.toast(_("Access granted. Log out and back in to see all entries."))

        self.operations.run(
            manager, manager.grant_journal_access, on_success=granted, error_heading=_("Could Not Allow Access")
        )

    def _render_simple(self, shown, flagged, flt) -> None:
        self._render_gen += 1
        gen = self._render_gen
        widgets.clear(self._simple)
        severity, none_found = CARD_FILTERS.get(flt, (None, _("No problems found in this range")))
        issues = [i for i in self.issues if severity in (None, i.severity)]
        selected = self.selected
        # The same problem after a refetch is a new object with the same id.
        if isinstance(selected, Issue):
            selected = next((i for i in issues if i.id == selected.id), None)
            self._select(selected)
        elif selected is not None and not any(e is selected for _i, e in shown[: self._shown]):
            selected = None
            self._select(None)
        problems = timeline_list = None
        if issues:
            n = len(issues)
            section = widgets.Section(
                ngettext("{n} problem needs your attention", "{n} problems need your attention", n).format(n=n),
                _("Found by scanning the journal for errors and repeated warnings"),
                title_css="error",
            )
            problems = section.list
            for issue in issues:
                row = IssueRow(issue)
                problems.append(row)
                if issue is selected:
                    problems.select_row(row)
            self._simple.append(section.box)
        else:
            ok = Gtk.Box(spacing=12, css_classes=["ok-banner"])
            ok.append(widgets.dot("running"))
            ok.append(widgets.label(none_found, "success", "unit-title"))
            self._simple.append(ok)

        title = _pick(FILTERS, flt)[3]
        timeline = widgets.Section(title, _("Newest first"))
        timeline_list = timeline.list
        for listbox, other in ((problems, timeline_list), (timeline_list, problems)):
            if listbox is not None:
                listbox.set_selection_mode(Gtk.SelectionMode.SINGLE)
                listbox.connect("row-selected", self._on_row_selected, other)
                listbox.connect("row-activated", self._on_row_activated)
        self._simple.append(timeline.box)
        self.stack.set_visible_child_name("simple")
        if self._jump_to_timeline:
            self._jump_to_timeline = False
            self._scroll_to(timeline.box, gen)

        if not shown:
            message = widgets.no_results(self._query) if self._query else _("No entries")
            timeline.list.append(widgets.placeholder_row(message))
            return

        # Paint the timeline in chunks so switching to Journal does not stall.
        target = min(len(shown), self._shown)
        state = {"pos": 0, "last_boot": None}

        def add_chunk():
            if gen != self._render_gen:
                return GLib.SOURCE_REMOVE
            end = min(state["pos"] + CHUNK, target)
            for index, entry in shown[state["pos"] : end]:
                if entry.boot_id and entry.boot_id != state["last_boot"]:
                    timeline.list.append(self._boot_row(entry.boot_id))
                    state["last_boot"] = entry.boot_id
                issue = flagged.get(index)
                badge = css = tooltip = ""
                if issue:
                    badge = issue_badge(issue)
                    css = "error" if issue.severity == ERROR else "warning"
                    tooltip = issue_text(issue)[0]
                row = widgets.log_row(entry, badge=badge, badge_css=css, badge_tooltip=tooltip, activatable=True)
                timeline.list.append(row)
                if entry is selected:
                    timeline.list.select_row(row)
            state["pos"] = end
            if state["pos"] < target:
                return GLib.SOURCE_CONTINUE
            if len(shown) > self._shown:
                more = Gtk.Button(
                    label=_("Show More"),
                    halign=Gtk.Align.CENTER,
                    css_classes=["flat"],
                    margin_top=6,
                    margin_bottom=6,
                )
                more.connect("clicked", self._show_more)
                timeline.list.append(Gtk.ListBoxRow(child=more, activatable=False, selectable=False))
            return GLib.SOURCE_REMOVE

        GLib.idle_add(add_chunk)

    def _scroll_to(self, widget: Gtk.Widget, gen: int) -> None:
        """Scroll the timeline's heading to the top, without first painting the top of the page.

        ``widget`` is made at least a page tall, or a short list would stop at
        the bottom. Its place is measured before the first layout, since the new
        page would otherwise show its first frame scrolled to the top. Then, as
        a fallback, keep correcting until it gets there, for a few seconds at most.
        """
        adjustment = self._simple_scroll.get_vadjustment()
        page = adjustment.get_page_size()
        width = self._simple.get_width()
        if page and width:
            widget.set_size_request(-1, int(page))
            above = 0
            child = self._simple.get_first_child()
            while child is not widget:
                above += child.measure(Gtk.Orientation.VERTICAL, width)[1] + self._simple.get_spacing()
                child = child.get_next_sibling()
            # The page keeps this once laid out, as the timeline is now tall enough to allow it.
            adjustment.configure(
                above,
                adjustment.get_lower(),
                max(adjustment.get_upper(), above + page),
                adjustment.get_step_increment(),
                adjustment.get_page_increment(),
                page,
            )
        deadline = {}

        def tick(_scroll, clock):
            now = clock.get_frame_time()
            if gen != self._render_gen or now > deadline.setdefault("at", now + 3 * 1_000_000):
                return GLib.SOURCE_REMOVE
            if not widget.get_height():
                return GLib.SOURCE_CONTINUE  # not allocated until the next frame
            ok, point = widget.compute_point(self._simple, Graphene.Point())
            if not ok:
                return GLib.SOURCE_REMOVE
            page = int(adjustment.get_page_size())
            if widget.get_height() < page:
                widget.set_size_request(-1, page)
                return GLib.SOURCE_CONTINUE  # taller from the next frame
            adjustment.set_value(point.y)
            return GLib.SOURCE_REMOVE if adjustment.get_value() >= point.y - 1 else GLib.SOURCE_CONTINUE

        self._simple_scroll.add_tick_callback(tick)

    def _show_more(self, _button):
        position = self._simple_scroll.get_vadjustment().get_value()
        self._shown += PAGE
        self._refresh()
        GLib.idle_add(lambda: self._simple_scroll.get_vadjustment().set_value(position) and False)

    def _boot_row(self, boot_id: str) -> Gtk.ListBoxRow:
        box = Gtk.Box(spacing=10, css_classes=["boot-header"])
        box.append(Gtk.Image(icon_name="computer-symbolic"))
        current = boot_id == self._boot_id
        box.append(widgets.label(_("This boot") if current else _("Earlier boot · {id}").format(id=boot_id[:8])))
        return Gtk.ListBoxRow(child=box, activatable=False, selectable=False)
