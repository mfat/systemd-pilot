#!/usr/bin/env python3
"""Measure startup and reload cost on this machine (needs a display or xvfb-run).

python3 tests/benchmark_load.py
meson devenv -C build python3 tests/benchmark_load.py
"""

from __future__ import annotations

import os
import statistics
import sys
import tempfile
import time
from pathlib import Path

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root / "src"))

# Isolated config so the benchmark does not touch the user's hosts or passwords.
_home = tempfile.mkdtemp(prefix="systemd-pilot-bench-")
os.environ["HOME"] = _home
os.environ["XDG_CONFIG_HOME"] = os.path.join(_home, ".config")

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib  # noqa: E402

Adw.init()

from systemdpilot.core.hosts import HostStore  # noqa: E402
from systemdpilot.core.known_hosts import KnownHosts  # noqa: E402
from systemdpilot.core.local import LocalRunner  # noqa: E402
from systemdpilot.core.manager import SystemdManager  # noqa: E402
from systemdpilot.core.models import Scope  # noqa: E402
from systemdpilot.core.secrets import MemorySecretStore  # noqa: E402
from systemdpilot.core.session import Sessions  # noqa: E402
from systemdpilot.ui.settings import Settings  # noqa: E402
from systemdpilot.ui.window import Window  # noqa: E402


def pump(n: int = 30) -> None:
    ctx = GLib.MainContext.default()
    for _ in range(n):
        while ctx.iteration(False):
            pass


def backend_report(manager: SystemdManager, scope: Scope) -> None:
    print("\n=== Backend (worker thread) ===")
    units = manager.list_units(scope, False)

    def time_call(label: str, fn):
        times = []
        for _ in range(3):
            manager.invalidate_unit_files()
            t0 = time.perf_counter()
            fn()
            times.append(time.perf_counter() - t0)
        print(
            f"  {label:32s}  med={statistics.median(times):.3f}s  "
            f"(2nd call cached: ", end=""
        )
        t0 = time.perf_counter()
        fn()
        print(f"{time.perf_counter() - t0:.3f}s)")

    time_call("list_units (active)", lambda: manager.list_units(scope, False))
    time_call("attach_file_states (inactive)", lambda: manager.attach_file_states(units, True))
    time_call("add_runtime", lambda: manager.add_runtime(units))
    t0 = time.perf_counter()
    manager.journal(since="24 hours ago", lines=1500)
    print(f"  {'journal 1500/24h':32s}  {time.perf_counter() - t0:.3f}s")
    print(f"  active services listed: {len(units)}")
    print("  (attach_file_states only runs when “Show Inactive” is on)")


def ui_report() -> None:
    print("\n=== UI (GTK main thread) ===")
    manager = SystemdManager(LocalRunner())
    scope = Scope.SYSTEM
    units = manager.list_units(scope, False)
    results: dict[str, float] = {}

    app = Adw.Application(application_id="io.github.mfat.systemdpilot.benchmark")

    def activate(app):
        from systemdpilot.core.paths import app_config_dir

        config = app_config_dir()
        config.mkdir(parents=True, exist_ok=True)
        sessions = Sessions(HostStore(config, MemorySecretStore()), KnownHosts(config / "known_hosts"))

        t0 = time.perf_counter()
        win = Window(application=app, sessions=sessions, settings=Settings())
        results["window_init"] = time.perf_counter() - t0
        win.present()
        pump(40)

        t0 = time.perf_counter()
        win._on_units_loaded(units)
        pump(80)
        results["first_paint_simple_list"] = time.perf_counter() - t0

        t0 = time.perf_counter()
        win.reload()
        deadline = time.perf_counter() + 15
        while time.perf_counter() < deadline and win.content_stack.get_visible_child_name() == "loading":
            pump(5)
        # Wait for list-units to paint (no list-unit-files on the default path).
        while time.perf_counter() < deadline and not win.services.units:
            pump(5)
        results["reload_default_wall"] = time.perf_counter() - t0
        pump(40)

        print(f"  {'window_init':32s}  {results['window_init']:.3f}s")
        print(f"  {'first_paint_simple_list':32s}  {results['first_paint_simple_list']:.3f}s")
        print(f"  {'reload (default, no file states)':32s}  {results['reload_default_wall']:.3f}s")
        app.quit()

    app.connect("activate", activate)
    GLib.timeout_add_seconds(60, lambda: (print("UI benchmark timed out"), app.quit(), False)[-1])
    app.run([])


def main() -> int:
    print("systemd Pilot load benchmark")
    print(f"DISPLAY={os.environ.get('DISPLAY', '(unset)')}")
    backend_report(SystemdManager(LocalRunner()), Scope.SYSTEM)
    ui_report()
    print("\nNotes:")
    print("  • Default load is list-units only (~15ms); enable state is only in the details dialog.")
    print("  • list-unit-files runs only with “Show Inactive” (to list unloaded units).")
    print("  • add_runtime runs only for the service whose details are open.")
    print("  • List rows are built in idle chunks; Journal badge is deferred ~1.5s after units settle.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
