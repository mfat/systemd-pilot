#!/usr/bin/env python3
"""Run systemd Pilot straight from the source checkout, without building.

python3 run.py [-v]
"""

import gettext
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

root = Path(__file__).resolve().parent
sys.path.insert(0, str(root / "src"))
gettext.textdomain("systemd-pilot")

match = re.search(r"^\s*version:\s*'([^']+)'", (root / "meson.build").read_text(), re.MULTILINE)
version = f"{match.group(1)}-dev" if match else "dev"


def register_resources() -> None:
    """Compile and register the GResource bundle, as an installed copy has it.

    It carries the app icon too. Without glib-compile-resources, the UI files
    and CSS are read from the source tree and the icon comes from the system.
    """
    tool = shutil.which("glib-compile-resources")
    if not tool:
        return
    import gi

    gi.require_version("Gio", "2.0")
    from gi.repository import Gio, GLib

    src = root / "src"
    fd, target = tempfile.mkstemp(suffix=".gresource")
    os.close(fd)
    try:
        done = subprocess.run(
            [tool, "--sourcedir", str(src), "--target", target, str(src / "systemdpilot.gresource.xml")],
            capture_output=True,
            text=True,
        )
        if done.returncode != 0:
            print(f"Using the source files: {done.stderr.strip()}", file=sys.stderr)
            return
        Gio.Resource.load(target)._register()  # mapped into memory; the file can go
    except GLib.Error as error:
        print(f"Using the source files: {error.message}", file=sys.stderr)
    finally:
        os.unlink(target)


register_resources()

from systemdpilot.main import main  # noqa: E402

sys.exit(main(version))
