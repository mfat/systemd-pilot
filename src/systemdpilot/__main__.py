"""``python3 -m systemdpilot``, with ``src`` on the path.

Uses the GResource bundle from a meson build tree when
SYSTEMD_PILOT_RESOURCE is set (``meson devenv`` does this), and the
source files otherwise.
"""

import os
import sys

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio  # noqa: E402

resource = os.environ.get("SYSTEMD_PILOT_RESOURCE")
if resource:
    Gio.Resource.load(resource)._register()

from systemdpilot.main import main  # noqa: E402

sys.exit(main(os.environ.get("SYSTEMD_PILOT_VERSION", "dev")))
