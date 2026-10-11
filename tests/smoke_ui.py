"""Start the app, open every dialog and quit. Needs a display (e.g. xvfb-run).

meson devenv -C build python3 tests/smoke_ui.py   # with the GResource bundle
python3 tests/smoke_ui.py                         # from the source files
"""

import os
import sys
import tempfile
import traceback

# Run in a throwaway home, so the user's hosts, the 3.x host import and
# ~/.ssh are never touched. Must happen before GLib reads these variables.
_home = tempfile.mkdtemp(prefix="systemd-pilot-smoke-")
os.environ["HOME"] = _home
os.environ["XDG_CONFIG_HOME"] = os.path.join(_home, ".config")

import gi  # noqa: E402 - must follow the HOME override above

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib  # noqa: E402

if os.environ.get("SYSTEMD_PILOT_RESOURCE"):
    Gio.Resource.load(os.environ["SYSTEMD_PILOT_RESOURCE"])._register()
else:
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from systemdpilot.core.models import AuthMethod, Host, Scope, Unit  # noqa: E402
from systemdpilot.main import Application  # noqa: E402
from systemdpilot.ui.unit_dialog import UnitDialog  # noqa: E402

errors = []
progress = {"step": 0}
VERBOSE = bool(os.environ.get("SMOKE_VERBOSE"))


def excepthook(*exc_info):
    errors.append(exc_info)
    traceback.print_exception(*exc_info)


sys.excepthook = excepthook
app = Application("smoke-test")


def run_steps(window):
    unit = Unit("smoke-test.service", "Smoke test", "loaded", "inactive", "dead", "disabled")
    steps = [
        window.create_unit,
        lambda: window.get_visible_dialog().close(),
        lambda: check_create_dialog(window),
        lambda: check_create_conflicts(window),
        lambda: check_edit_dialogs(window),
        window.add_host,
        lambda: window.get_visible_dialog().close(),
        # The services view always has a details column; a service opens there and has no close button.
        lambda: check(window.details_split.get_show_sidebar(), "no details column"),
        lambda: check(window.details_bin.get_child() is window._details_placeholder, "no placeholder"),
        lambda: window.show_unit(unit),
        lambda: check(not isinstance(window.get_visible_dialog(), UnitDialog), "details opened as a dialog"),
        lambda: check(window.details_bin.get_child() is window._details, "details panel not shown"),
        lambda: check(not hasattr(window._details, "close_button"), "details panel has a close button"),
        # System and user services share one list; the same name can be both.
        lambda: same_name_in_both_scopes(window),
        lambda: window.activate_action("win.show-inactive", None),
        lambda: window.activate_action("win.search", None),
        # The redesign: filters, the journal and its problems.
        # One step, so a service list loading in the background can't replace the units in between.
        lambda: filter_failed(window),
        lambda: window.services.activate_action("services.filter", GLib.Variant("s", "all")),
        lambda: window.show_unit(unit),
        lambda: check(window._details.stack.get_visible_child_name() == "overview", "no overview page"),
        # Log shows its entries as rows, or raw as journalctl prints it.
        lambda: window._details.stack.set_visible_child_name("activity"),
        lambda: window._details.raw_button.set_active(True),
        lambda: check(window._details.activity_stack.get_visible_child_name() == "raw", "no raw log"),
        lambda: window._details.raw_button.set_active(False),
        lambda: check(window._details.activity_stack.get_visible_child_name() == "list", "no activity list"),
        lambda: window._close_details(),
        lambda: check(window._details is None, "details panel not closed"),
        # The bell switches the window to the journal: its filters, list and details.
        lambda: window.activate_action("win.journal", None),
        lambda: check(window.journal_shown, "journal not shown"),
        lambda: check(window.mode_dropdown.get_selected() == 1, "dropdown does not say Journal"),
        lambda: check(window.filters_bin.get_child() is window.journal.sidebar, "no journal filters in the sidebar"),
        lambda: check(window.details_bin.get_child() is window.journal_details, "no journal details"),
        lambda: window.journal.activate_action("journal.preset", GLib.Variant("s", "ssh")),
        lambda: window.journal.activate_action("journal.filter", GLib.Variant("s", "all")),
        lambda: window.journal.activate_action("journal.preset", GLib.Variant("s", "")),
        lambda: select_journal_entries(window),
        # A custom range: picked in a dialog, then shown on the time picker.
        lambda: window.journal._since.set_value("custom"),
        lambda: check(window.get_visible_dialog() is not None, "no custom range dialog"),
        lambda: window.get_visible_dialog()._apply.emit("clicked"),
        lambda: check(window.journal._state("since") == "custom", "custom range not applied"),
        lambda: check(window.journal._since._button_text.get("custom"), "custom range not shown on the picker"),
        # Choosing it again changes it.
        lambda: window.journal._since.emit("reselected", "custom"),
        lambda: check(window.get_visible_dialog() is not None, "custom range cannot be changed"),
        lambda: window.get_visible_dialog().close(),
        lambda: window.journal._since.set_value("24h"),
        lambda: check(window.journal._state("since") == "24h", "custom range still applied"),
        # Hidden entries: the banner offers access, which asks first.
        lambda: show_hidden_entries(window),
        lambda: check(isinstance(window.get_visible_dialog(), Adw.AlertDialog), "no access confirmation"),
        lambda: answer(window.get_visible_dialog(), "cancel"),
        # The dropdown switches back.
        lambda: window.mode_dropdown.set_selected(0),
        lambda: check(not window.journal_shown, "journal not closed"),
        lambda: check(window.filters_bin.get_child() is window.services.sidebar, "no service filters in the sidebar"),
        lambda: check(window.content_stack.get_visible_child_name() == "main", "services list not shown"),
        # Enable/disable is only in the details dialog now.
        lambda: check_no_enable_switch(window),
        # Remove a host from its edit dialog: confirm first, then the dialog closes.
        lambda: add_demo_host(window),
        window.edit_host,
        lambda: window.get_visible_dialog().on_remove_clicked(None),
        lambda: check(window.get_visible_dialog() is not None, "edit dialog closed before confirming"),
        lambda: answer(find_alert(window), "confirm"),
        lambda: check(not window.sessions.hosts.hosts(), "host was not removed"),
        lambda: check(window.get_visible_dialog() is None, "edit dialog still open after removing"),
        app.quit,
    ]

    def check(condition, message):
        if not condition:
            raise AssertionError(message)

    def create_dialog(window, runner, **kwargs):
        """A create dialog that writes through ``runner``, without a background thread."""
        from systemdpilot.core.manager import SystemdManager
        from systemdpilot.ui.create_unit_dialog import CreateUnitDialog

        class Operations:
            def run(self, manager, func, *, on_success=None, on_error=None, on_finish=None, error_heading=""):
                try:
                    result = func()
                except Exception as error:  # noqa: BLE001 - as Operations does
                    if not (on_error and on_error(error)):
                        raise
                else:
                    if on_success:
                        on_success(result)
                if on_finish:
                    on_finish()

        created = []
        scope = kwargs.pop("scope", None)
        manager = SystemdManager(runner)
        manager._unit_files = {Scope.SYSTEM: {"taken.timer": "enabled"}}
        dialog = CreateUnitDialog(manager, scope, "test", Operations(), on_created=created.append, **kwargs)
        dialog.present(window)
        wait_shown(dialog)
        return dialog, created

    def wait_shown(dialog):
        """Until ``dialog`` has opened. libadwaita 1.5 opens it on its second frame
        after being shown, and closing it before then does nothing."""
        ticks = []
        dialog.add_tick_callback(lambda *_: ticks.append(None) or len(ticks) < 3)
        context = GLib.MainContext.default()
        deadline = GLib.get_monotonic_time() + 5 * GLib.USEC_PER_SEC
        while len(ticks) < 3 and GLib.get_monotonic_time() < deadline:
            context.iteration(False)

    def fake_runner(*codes):
        """Answers each command with the next exit status (0 once they run out), recording calls."""
        from systemdpilot.core.runner import CommandResult, CommandRunner

        class Runner(CommandRunner):
            label = "fake"
            calls = []

            def run(self, argv, *, input=None, privileged=False, timeout=60):
                self.calls.append({"argv": list(argv), "input": input, "privileged": privileged})
                code = codes[len(self.calls) - 1] if len(self.calls) <= len(codes) else 0
                stderr = {17: "/etc/systemd/system/backup.timer\n", 18: "Access denied\n"}.get(code, "")
                return CommandResult(tuple(argv), code, "", stderr)

        return Runner()

    def editor_titles(dialog):
        return [page.get_title() for _type, _buffer, page in dialog._editors]

    def editor_text(dialog, unit_type):
        buffer = next(b for t, b, _p in dialog._editors if t == unit_type)
        return buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), True)

    def select_template(dialog, template_id):
        from systemdpilot.core.templates import TEMPLATES

        dialog.template_row.set_selected(next(i for i, t in enumerate(TEMPLATES) if t.id == template_id))

    def check_create_dialog(window):
        runner = fake_runner()
        dialog, created = create_dialog(window, runner)
        check(not dialog.program_row.get_visible(), "program picker offered for a machine that is not this one")
        check(not dialog.create_button.get_sensitive(), "create allowed without a name")
        check(not dialog.editor_switcher.get_visible(), "file tabs shown for a single file")

        select_template(dialog, "timer")
        check(len(dialog._editors) == 2 and dialog.editor_switcher.get_visible(), "timer template has no two files")
        dialog.name_row.set_text("backup")
        check(editor_titles(dialog) == ["backup.service", "backup.timer"], f"tabs named {editor_titles(dialog)}")
        check(dialog.create_button.get_sensitive(), "a good name is refused")
        dialog.name_row.set_text("backup.socket")
        check(dialog.name_row.has_css_class("error"), "another unit type is not flagged")
        check(not dialog.create_button.get_sensitive(), "create allowed for another unit type")
        dialog.name_row.set_text("taken")
        check(dialog.name_row.has_css_class("warning"), "an existing unit is not flagged")
        check("taken.timer" in dialog.name_row.get_title(), "the existing unit is not named")
        check(dialog.create_button.get_sensitive(), "create refused for an existing unit; it should ask instead")
        dialog.name_row.set_text("backup.timer")
        check(not dialog.name_row.has_css_class("warning"), "warning left after renaming")

        dialog.set_program("/opt/My App/run")
        check('ExecStart="/opt/My App/run"\n' in editor_text(dialog, "service"), "program not set")
        check(dialog.editor_stack.get_visible_child() is dialog._editors[0][2].get_child(), "service tab not shown")

        # The files were edited, so switching template asks first; cancelling keeps everything.
        select_template(dialog, "simple")
        alert = window.get_visible_dialog()
        check(isinstance(alert, Adw.AlertDialog), "template replaced without asking")
        answer(alert, "cancel")
        check(dialog.template_row.get_selected() == 2, "template choice not put back after cancelling")
        check(len(dialog._editors) == 2, "files replaced after cancelling")
        check("/opt/My App/run" in editor_text(dialog, "service"), "edits lost after cancelling")

        dialog.enable_row.set_active(True)
        dialog.start_row.set_active(True)
        dialog.create_button.emit("clicked")
        call = runner.calls[-1]
        check(call["privileged"], "a system unit was written unprivileged")
        check(
            call["argv"][4:] == ["--system", "0", "1", "1", "backup.timer", "backup.service", "backup.timer"],
            f"unexpected install arguments {call['argv'][4:]}",
        )
        check(len(call["input"].splitlines()) == 2, "not both files were sent")
        check(len(runner.calls) == 1, "more than one privileged command")
        check(created == ["backup.timer"], f"created reported as {created}")
        check(window.get_visible_dialog() is None, "dialog left open after creating")

    def check_create_conflicts(window):
        # An existing file: asks, then replaces.
        runner = fake_runner(17)
        dialog, created = create_dialog(window, runner)
        select_template(dialog, "timer")
        dialog.name_row.set_text("backup")
        dialog.create_button.emit("clicked")
        alert = window.get_visible_dialog()
        check(isinstance(alert, Adw.AlertDialog) and "backup.timer" in alert.get_body(), "no replace question")
        check("backup.service, backup.timer" in alert.get_body(), "replace question does not say what is overwritten")
        check(not created, "reported as created though nothing was written")
        answer(alert, "confirm")
        check(runner.calls[-1]["argv"][5] == "1", "replacing did not overwrite")
        check(created == ["backup.timer"], "replaced unit not reported")

        # Created, but enabling failed: the dialog closes and the error says what happened.
        runner = fake_runner(18)
        dialog, created = create_dialog(window, runner, scope=Scope.USER)
        dialog.name_row.set_text("web")
        dialog.enable_row.set_active(True)
        dialog.create_button.emit("clicked")
        check(created == ["web.service"], "a unit that was written is not reported")
        check(not runner.calls[-1]["privileged"], "a user unit was written privileged")
        alert = window.get_visible_dialog()
        check(isinstance(alert, Adw.Dialog) and "Enable" in alert.get_title(), "enable failure not shown")
        wait_shown(alert)
        alert.close()
        check(window.get_visible_dialog() is None, f"left open: {window.get_visible_dialog()}")

    def check_edit_dialogs(window):
        runner = fake_runner()
        dialog, created = create_dialog(
            window, runner, scope=Scope.USER, edit_name="demo.timer", edit_content="[Timer]\nOnCalendar=daily\n"
        )
        hidden = (dialog.scope_row, dialog.template_row, dialog.program_row, dialog.enable_row, dialog.start_row)
        check(not any(row.get_visible() for row in hidden), "editing shows choices for new units")
        check(len(dialog._editors) == 1 and not dialog.editor_switcher.get_visible(), "editing shows several files")
        check(dialog.create_button.get_sensitive(), "save not allowed")
        dialog.create_button.emit("clicked")
        argv = runner.calls[-1]["argv"]
        check(argv[4:] == ["--user", "1", "0", "0", "", "demo.timer"], f"unexpected save arguments {argv[4:]}")
        check(created == ["demo.timer"], "save not reported")

        runner = fake_runner()
        dialog, created = create_dialog(
            window,
            runner,
            scope=Scope.SYSTEM,
            edit_name="demo.service",
            edit_content="[Service]\nNice=5\n",
            override=True,
        )
        dialog.create_button.emit("clicked")
        check(any("override.conf" in arg for arg in runner.calls[-1]["argv"]), "override not written as a drop-in")
        check(created == ["demo.service"], "override save not reported")

    def simple_units(window):
        from systemdpilot.ui.services_view import ServiceItem

        model = window.services.model
        items = (model.get_item(i) for i in range(model.get_n_items()))
        return [item.unit for item in items if isinstance(item, ServiceItem)]

    def same_name_in_both_scopes(window):
        running = dict(load_state="loaded", active_state="active", sub_state="running")
        window.services.set_units([Unit("dbus.service", **running), Unit("dbus.service", **running, scope=Scope.USER)])
        units = simple_units(window)
        check(sorted(u.scope.value for u in units) == ["system", "user"], "a same-named service is missing")

    def check_no_enable_switch(window):
        from gi.repository import Gtk

        window.services.set_units(demo_units())
        check(simple_units(window), "no simple service rows")
        has_switch = any(isinstance(w, Gtk.Switch) for w in _descendants(window.services.simple_list))
        check(not has_switch, "list still has enable switches")

    def _descendants(widget):
        child = widget.get_first_child()
        while child:
            yield child
            yield from _descendants(child)
            child = child.get_next_sibling()

    def filter_failed(window):
        window.services.set_units(demo_units())
        window.services.activate_action("services.filter", GLib.Variant("s", "failed"))
        check(window.services.visible_count == 1, "failed filter did not apply")

    def select_journal_entries(window):
        from datetime import datetime

        from systemdpilot.core.models import LogEntry, LogResult

        journal = window.journal
        now = datetime.now()
        entries = [
            LogEntry(now, 3, "broken", "7", "Failed with result 'exit-code'.", unit="broken.service"),
            LogEntry(now, 6, "kernel", "", "usb 1-1: new device\nsecond line", kernel=True),
        ]
        journal._result = LogResult(entries)
        journal._analysis_key = None
        journal._refresh()
        check(window.content_stack.get_visible_child_name() == "journal", "journal list not shown")
        journal.emit("selected", entries[1])
        check(window.journal_details.item is entries[1], "entry details not shown")
        if journal.issues:
            journal.emit("selected", journal.issues[0])
            check(window.journal_details.item is journal.issues[0], "problem details not shown")

    def show_hidden_entries(window):
        from systemdpilot.core.models import LogResult

        journal = window.journal
        journal._result, journal._access = LogResult([], "hidden"), "missing"
        journal._refresh()
        check(journal._banner.get_revealed(), "no banner for hidden entries")
        check(journal._banner.get_button_label() == "Allow Access…", "banner offers no access")
        journal._banner.emit("button-clicked")

    def demo_units():
        return [
            unit,
            Unit("ok.service", "Fine", "loaded", "active", "running", "enabled", main_pid=42, memory=5 << 20),
            Unit("once.service", "Once", "loaded", "active", "exited", "static"),
            Unit("broken.service", "Broken", "loaded", "failed", "failed", "enabled"),
        ]

    def add_demo_host(window):
        host = Host("demo", "demo.invalid", "me", auth=AuthMethod.AGENT)
        window.sessions.hosts.save(host)
        window._rebuild_machine_list()
        window.machine_list.select_row(window._row_for(host.id))

    def answer(alert, response):
        wait_shown(alert)
        alert.emit("response", response)
        alert.close()

    def find_alert(window):
        # The confirmation is presented over the edit dialog.
        dialog = window.get_visible_dialog()
        assert isinstance(dialog, Adw.AlertDialog), f"expected a confirmation, got {dialog}"
        return dialog

    total = len(steps)

    def next_step():
        step = steps.pop(0)
        progress["step"] = total - len(steps)
        if VERBOSE:
            print(f"step {progress['step']}/{total}", flush=True)
        try:
            step()
        except Exception:  # noqa: BLE001 - any failure fails the smoke test
            excepthook(*sys.exc_info())
            app.quit()
            return GLib.SOURCE_REMOVE
        return GLib.SOURCE_CONTINUE if steps else GLib.SOURCE_REMOVE

    GLib.timeout_add(300, next_step)


def on_timeout():
    # Name the step that never finished; a bare exit status says nothing.
    print(f"UI smoke test timed out at step {progress['step'] or 'startup'}", file=sys.stderr)
    errors.append("timeout")
    app.quit()


app.connect("activate", lambda a: GLib.idle_add(lambda: run_steps(a.props.active_window)) and None)
GLib.timeout_add_seconds(60, on_timeout)
app.run([])
if errors:
    sys.exit(1)
print("UI smoke test passed")
