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
        lambda: window._details.raw_switch.set_active(True),
        lambda: check(window._details.activity_stack.get_visible_child_name() == "raw", "no raw log"),
        lambda: window._details.raw_switch.set_active(False),
        lambda: check(window._details.activity_stack.get_visible_child_name() == "list", "no activity list"),
        lambda: window._close_details(),
        lambda: check(window._details is None, "details panel not closed"),
        # The journal is pushed over the whole window.
        lambda: window.activate_action("win.journal", None),
        lambda: check(window.journal_shown, "journal page not shown"),
        lambda: window.journal.activate_action("journal.preset", GLib.Variant("s", "ssh")),
        lambda: window.journal.activate_action("journal.filter", GLib.Variant("s", "all")),
        lambda: window.journal.activate_action("journal.preset", GLib.Variant("s", "")),
        # Hidden entries: the banner offers access, which asks first.
        lambda: show_hidden_entries(window),
        lambda: check(isinstance(window.get_visible_dialog(), Adw.AlertDialog), "no access confirmation"),
        lambda: answer(window.get_visible_dialog(), "cancel"),
        lambda: window.nav_view.pop(),
        lambda: check(not window.journal_shown, "journal page not closed"),
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
