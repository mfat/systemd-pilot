"""Run from a build tree: ``meson devenv -C build python3 -m systemdpilot``."""

import os
import sys

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio  # noqa: E402

resource = os.environ.get("SYSTEMD_PILOT_RESOURCE")
if not resource:
    sys.exit("SYSTEMD_PILOT_RESOURCE is not set; run this through “meson devenv -C build”.")
Gio.Resource.load(resource)._register()

from systemdpilot.main import main  # noqa: E402

sys.exit(main(os.environ.get("SYSTEMD_PILOT_VERSION", "dev")))
