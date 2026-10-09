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
from gi.repository import Gio, GLib  # noqa: E402

if os.environ.get("SYSTEMD_PILOT_RESOURCE"):
    Gio.Resource.load(os.environ["SYSTEMD_PILOT_RESOURCE"])._register()
else:
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from systemdpilot.core.models import AuthMethod, Host, Unit  # noqa: E402
from systemdpilot.main import Application  # noqa: E402

errors = []


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
        lambda: window.show_unit(unit),
        lambda: window.get_visible_dialog().close(),
        lambda: window.activate_action("win.scope", GLib.Variant("s", "user")),
        lambda: window.activate_action("win.show-inactive", None),
        lambda: window.activate_action("win.search", None),
        # Context menu from the keyboard (Menu / Shift+F10).
        # One step, so a service list loading in the background can't replace the row in between.
        lambda: open_context_menu_from_keyboard(window, unit),
        lambda: window.unit_list._menu.popdown(),
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

    def open_context_menu_from_keyboard(window, unit):
        window.unit_list.set_units([unit])  # a known row, whatever services the machine has
        window.unit_list.select_name(unit.name)
        check(window.unit_list._popup_for_focus(), "keyboard context menu did not open")

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
        from gi.repository import Adw

        dialog = window.get_visible_dialog()
        assert isinstance(dialog, Adw.AlertDialog), f"expected a confirmation, got {dialog}"
        return dialog

    def next_step():
        step = steps.pop(0)
        try:
            step()
        except Exception:  # noqa: BLE001 - any failure fails the smoke test
            excepthook(*sys.exc_info())
            app.quit()
            return GLib.SOURCE_REMOVE
        return GLib.SOURCE_CONTINUE if steps else GLib.SOURCE_REMOVE

    GLib.timeout_add(300, next_step)


app.connect("activate", lambda a: GLib.idle_add(lambda: run_steps(a.props.active_window)) and None)
GLib.timeout_add_seconds(60, lambda: (errors.append("timeout"), app.quit()))
app.run([])
if errors:
    sys.exit(1)
print("UI smoke test passed")
