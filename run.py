#!/usr/bin/env python3
"""Run systemd Pilot straight from the source checkout, without building.

python3 run.py [-v]
"""

import gettext
import re
import sys
from pathlib import Path

root = Path(__file__).resolve().parent
sys.path.insert(0, str(root / "src"))
gettext.textdomain("systemd-pilot")

match = re.search(r"^\s*version:\s*'([^']+)'", (root / "meson.build").read_text(), re.MULTILINE)
version = f"{match.group(1)}-dev" if match else "dev"

from systemdpilot.main import main  # noqa: E402

sys.exit(main(version))
