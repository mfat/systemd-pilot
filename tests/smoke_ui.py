"""Start the app, open every dialog and quit. Needs a display (e.g. xvfb-run).

meson devenv -C build python3 tests/smoke_ui.py
"""

import os
import sys
import traceback

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gio, GLib  # noqa: E402

Gio.Resource.load(os.environ["SYSTEMD_PILOT_RESOURCE"])._register()

from systemdpilot.core.models import Unit  # noqa: E402
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
        app.quit,
    ]

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
