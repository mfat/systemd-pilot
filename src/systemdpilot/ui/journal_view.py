"""The journal page: problems found in the system log, then the log itself."""

from __future__ import annotations

from collections.abc import Callable
from gettext import gettext as _
from gettext import ngettext

from gi.repository import Adw, Gio, GLib, GObject, Gtk, Pango

from ..core.journal import ERROR, PRESETS, PRESETS_BY_ID, Issue, find_issues
from ..core.manager import ACCESS_MISSING, ACCESS_PENDING, SystemdManager
from ..core.models import LogEntry, LogResult
from ..core.ssh import SSHRunner
from . import prompts, text, widgets, words
from .operations import Operations, describe
from .services_view import mode_switch
from .tasks import run_in_thread
from .widgets import Chip, Option, OptionButton

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
FILTERS = (
    ("problems", _("Flagged"), None, _("Flagged entries")),
    ("errors", _("Errors"), "failed", _("Errors")),
    ("warnings", _("Warnings"), "warning", _("Warnings")),
    ("all", _("All entries"), None, _("All entries")),
)
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
PRIORITY_FLAGS = {"problems": "-p warning", "errors": "-p err", "warnings": "-p warning..warning", "all": ""}

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


class IssueRow(Gtk.ListBoxRow):
    def __init__(self, issue: Issue, open_unit: Callable[[str], None] | None):
        super().__init__(activatable=False)
        title, explanation = issue_text(issue)
        error = issue.severity == ERROR
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box = Gtk.Box(spacing=14, margin_top=14, margin_bottom=14, margin_start=16, margin_end=16)
        icon = "dialog-error-symbolic" if error else "dialog-warning-symbolic"
        box.append(Gtk.Image(icon_name=icon, valign=Gtk.Align.START, css_classes=["error" if error else "warning"]))

        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3, hexpand=True)
        heading = Gtk.Box(spacing=8)
        heading.append(widgets.label(title, "unit-title", wrap=True))
        badge = Gtk.Label(label=_("Error") if error else _("Warning"), valign=Gtk.Align.CENTER)
        badge.set_css_classes(["badge", "error" if error else "warning"])
        heading.append(badge)
        texts.append(heading)
        if explanation:
            explanation = explanation.partition("\n")[0]
            texts.append(widgets.label(explanation, "issue-explanation", wrap=True))
        count = len(issue.entries)
        meta = " · ".join(
            (
                issue.unit or issue.source,
                ngettext("{n} entry", "{n} entries", count).format(n=count),
                _("last seen {ago}").format(ago=words.ago(issue.latest.timestamp)) if issue.latest.timestamp else "",
            )
        ).strip(" ·")
        texts.append(widgets.label(meta, "dim-label", "caption", wrap=True))
        box.append(texts)

        buttons = Gtk.Box(spacing=6, valign=Gtk.Align.START)
        if open_unit:
            open_button = Gtk.Button(label=_("Open Service"), css_classes=["small-pill"])
            open_button.connect("clicked", lambda *_: open_unit(issue.unit))
            buttons.append(open_button)
        toggle = Gtk.ToggleButton(css_classes=["flat", "small-pill"], tooltip_text=_("Show entries"))
        toggle_content = Gtk.Box(spacing=6)
        toggle_content.append(Gtk.Label(label=_("Entries")))
        chevron = Gtk.Image(icon_name="pan-down-symbolic")
        toggle_content.append(chevron)
        toggle.set_child(toggle_content)
        buttons.append(toggle)
        box.append(buttons)
        outer.append(box)

        lines = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, css_classes=["issue-lines"])
        for entry in issue.entries[:30]:
            stamp = entry.timestamp.strftime("%b %d %H:%M:%S") if entry.timestamp else "—"
            source = entry.identifier + (f"[{entry.pid}]" if entry.pid else "")
            line = widgets.label(
                f"{stamp} {source}: {entry.message.partition(chr(10))[0]}",
                "monospace",
                "caption",
                wrap=True,
                wrap_mode=Pango.WrapMode.WORD_CHAR,
                selectable=True,
            )
            css = words.level(entry.priority)[1]
            if css:
                line.add_css_class(css)
            lines.append(line)
        if count > 30:
            lines.append(widgets.label(_("…and {n} more").format(n=count - 30), "dim-label", "caption"))
        revealer = Gtk.Revealer(child=lines)
        outer.append(revealer)

        def on_toggled(button):
            revealer.set_reveal_child(button.get_active())
            chevron.set_from_icon_name("pan-up-symbolic" if button.get_active() else "pan-down-symbolic")

        toggle.connect("toggled", on_toggled)
        self.set_child(outer)


class JournalView(Gtk.Box):
    """Emits ``open-unit`` (unit name) and ``changed`` when the summary or problem count changes."""

    __gtype_name__ = "SystemdPilotJournalView"
    __gsignals__ = {
        "open-unit": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
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
        self._mode = "simple"
        self._known_units: set[str] = set()
        self._shown = PAGE
        self.issues: list[Issue] = []
        self.entry_count = 0
        self.loaded = False
        self._loading = False
        self._stale = False
        self._needs_render = True  # False after a UI render matches the current analysis
        self._render_gen = 0  # cancels in-flight chunked timeline paints
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

        # Filters over the entries.
        bar = Gtk.Box(spacing=8, margin_top=12, margin_bottom=4, margin_start=24, margin_end=24)
        chips = Gtk.Box(spacing=6)
        self._chips: dict[str, Chip] = {}
        for value, label, dot_kind, _title in FILTERS:
            chip = Chip(label, "journal.filter", value, dot_kind)
            self._chips[value] = chip
            chips.append(chip)
        bar.append(chips)
        bar.append(
            Gtk.Separator(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.START, height_request=20, margin_top=6)
        )

        groups = []
        for group, heading in PRESET_GROUPS:
            options = [Option(p.id, *PRESET_TEXT[p.id]) for p in PRESETS if p.group == group]
            groups.append((heading, options))
        self._presets = OptionButton(
            "journal.preset",
            groups,
            intro=_("Ready-made searches for common problems. Counts use the range at the bottom."),
            width=420,
            counts=True,
        )
        self._presets.set_text(_("Presets"))
        self._clear_preset = Gtk.Button(
            icon_name="window-close-symbolic",
            tooltip_text=_("Remove Preset"),
            visible=False,
            css_classes=["chip", "preset-active", "preset-clear"],
            action_name="journal.preset",
            action_target=GLib.Variant("s", ""),
        )
        preset_box = Gtk.Box(css_classes=["linked"], valign=Gtk.Align.START)
        preset_box.append(self._presets)
        preset_box.append(self._clear_preset)
        bar.append(preset_box)
        bar.append(Gtk.Box(hexpand=True))
        switch = mode_switch()
        switch.set_valign(Gtk.Align.START)
        bar.append(switch)
        self.append(widgets.scroller(bar))

        self._banner = Adw.Banner()
        self._banner.connect("button-clicked", lambda *_: self._banner_action and self._banner_action())
        self.append(self._banner)

        self.stack = Gtk.Stack(vexpand=True, hhomogeneous=False, transition_type=Gtk.StackTransitionType.CROSSFADE)
        spinner = Gtk.Spinner(
            spinning=True, width_request=32, height_request=32, halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER
        )
        self.stack.add_named(spinner, "loading")

        self._simple = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=22, margin_top=8, margin_bottom=24)
        clamp = Adw.Clamp(
            child=self._simple, maximum_size=880, tightening_threshold=600, margin_start=24, margin_end=24
        )
        self._simple_scroll = Gtk.ScrolledWindow(child=clamp, hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.stack.add_named(self._simple_scroll, "simple")

        self._command = widgets.label(
            "", "monospace", "caption", "dim-label", "command-line", selectable=True, wrap=True
        )
        self._text = Gtk.TextView(
            editable=False, monospace=True, wrap_mode=Gtk.WrapMode.WORD_CHAR, css_classes=["output"]
        )
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, css_classes=["card", "table-card"])
        card.append(self._command)
        card.append(Gtk.Separator())
        card.append(Gtk.ScrolledWindow(child=self._text, vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER))
        card.set_margin_top(8)
        card.set_margin_bottom(24)
        card.set_margin_start(24)
        card.set_margin_end(24)
        self.stack.add_named(card, "advanced")

        self._status = Adw.StatusPage()
        self.stack.add_named(self._status, "status")
        self.append(self.stack)

        # The range the entries come from.
        footer = Gtk.Box(spacing=8, margin_top=8, margin_bottom=8, margin_start=24, margin_end=24)
        footer.append(widgets.label(_("Showing"), "dim-label"))
        self._since = OptionButton(
            "journal.since",
            [(None, [Option(v, label, "", f"--since “{flag}”" if flag else "") for v, label, _p, flag in SINCE])],
            css=("picker",),
        )
        footer.append(self._since)
        footer.append(widgets.label(_("in"), "dim-label"))
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
        footer.append(self._boot)
        footer.append(widgets.label(_("from"), "dim-label"))
        self._source = OptionButton(
            "journal.source",
            [(None, [Option(v, label, help, "-k" if kernel else "") for v, label, help, _t, kernel in SOURCES])],
            css=("picker",),
        )
        footer.append(self._source)
        for button in (self._since, self._boot, self._source):
            button.set_direction(Gtk.ArrowType.UP)
        footer_scroll = widgets.scroller(footer)
        footer_scroll.add_css_class("journal-footer")
        self.append(footer_scroll)
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

    def set_mode(self, mode: str) -> None:
        if mode == self._mode:
            return
        self._mode = mode
        if self.loaded and self._page_visible():
            self._refresh()
        else:
            self._needs_render = True

    def _page_visible(self) -> bool:
        """True when the journal stack page is the one on screen."""
        parent = self.get_parent()
        stack = parent.get_parent() if parent is not None else None
        return isinstance(stack, Gtk.Stack) and stack.get_visible_child() is parent

    def set_known_units(self, names: set[str]) -> None:
        self._known_units = names

    def summary(self) -> str:
        if not self.loaded:
            return _("Journal")
        parts = [
            _pick(SINCE, self._state("since"))[2],
            _pick(BOOTS, self._state("boot"))[3] if self._state("boot") != "all" else "",
            _("kernel only") if self._state("source") == "kernel" else "",
        ]
        rng = ", ".join(p for p in parts if p)
        count = ngettext("{n} entry", "{n} entries", self.entry_count).format(n=self.entry_count)
        admin = _("as administrator") if self._elevated else ""
        return " · ".join(p for p in (_("Journal"), count, rng, admin) if p)

    # -- internals --------------------------------------------------------

    def _state(self, name: str) -> str:
        return self._actions.lookup_action(name).get_state().get_string()

    def _on_option(self, action, value, name):
        if name == "preset" and value.get_string() == action.get_state().get_string():
            value = GLib.Variant("s", "")  # choosing the active preset again removes it
        action.set_state(value)
        if name == "preset" and value.get_string():
            self._actions.lookup_action("filter").set_state(GLib.Variant("s", "all"))
        self._update_pickers()
        if name in ("since", "boot", "source"):
            self.reload()
        else:
            self._shown = PAGE
            self._refresh()

    def _update_pickers(self) -> None:
        self._since.set_text(_pick(SINCE, self._state("since"))[1])
        self._boot.set_text(_pick(BOOTS, self._state("boot"))[3])
        self._source.set_text(_pick(SOURCES, self._state("source"))[3])

    def _refresh(self, *, ui: bool = True) -> None:
        if self._result is None:
            return
        query = self._query
        preset_id = self._state("preset")
        preset = PRESETS_BY_ID.get(preset_id)
        # Searching, changing preset, or a new fetch: re-scan. Filter/mode changes reuse this.
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
        for value, chip in self._chips.items():
            chip.set_count(self._filter_counts.get(value, 0))
        if preset:
            self._presets.set_text(f"{PRESET_TEXT[preset.id][0]}  {len(entries)}")
            self._presets.set_css_classes(["chip", "preset-active"])
        else:
            self._presets.set_text(_("Presets"))
            self._presets.set_css_classes(["chip"])
        self._clear_preset.set_visible(preset is not None)

        self._update_banner()

        flt = self._state("filter")
        shown = [(i, e) for i, e in enumerate(entries) if _FILTER_MATCH[flt](i, e, flagged)]
        if self._mode == "advanced":
            self._render_advanced(shown, flagged, preset)
        else:
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
        if self.issues:
            n = len(self.issues)
            section = widgets.Section(
                ngettext("{n} problem needs your attention", "{n} problems need your attention", n).format(n=n),
                _("Found by scanning the journal for errors and repeated warnings"),
                title_css="error",
            )
            for issue in self.issues:
                unit = issue.unit if issue.unit in self._known_units else ""
                section.list.append(IssueRow(issue, (lambda name: self.emit("open-unit", name)) if unit else None))
            self._simple.append(section.box)
        else:
            ok = Gtk.Box(spacing=12, css_classes=["ok-banner"])
            ok.append(widgets.dot("running"))
            ok.append(widgets.label(_("No problems found in this range"), "success", "unit-title"))
            self._simple.append(ok)

        title = _pick(FILTERS, flt)[3]
        timeline = widgets.Section(title, _("Newest first"))
        self._simple.append(timeline.box)
        self.stack.set_visible_child_name("simple")

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
                badge, css = ("", "")
                if issue:
                    badge, css = issue_text(issue)[0], "error" if issue.severity == ERROR else "warning"
                timeline.list.append(widgets.log_row(entry, badge=badge, badge_css=css))
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
                timeline.list.append(Gtk.ListBoxRow(child=more, activatable=False))
            return GLib.SOURCE_REMOVE

        GLib.idle_add(add_chunk)

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
        return Gtk.ListBoxRow(child=box, activatable=False)

    def _render_advanced(self, shown, flagged, preset) -> None:
        flt = self._state("filter")
        boot = _pick(BOOTS, self._state("boot"))[4]
        since = _pick(SINCE, self._state("since"))[3]
        flags = [
            "-b" if boot == 0 else (f"-b {boot}" if boot is not None else ""),
            "-k" if self._state("source") == "kernel" else "",
            f'--since "{since}"' if since else "",
            PRIORITY_FLAGS[flt],
            preset.command if preset else "",
        ]
        self._command.set_label("$ journalctl -r " + " ".join(f for f in flags if f))
        notes = {pos: issue_text(flagged[i])[0] for pos, (i, _e) in enumerate(shown) if i in flagged}
        text.set_journal(self._text.get_buffer(), [e for _i, e in shown], notes, self._boot_id)
        self.stack.set_visible_child_name("advanced")
