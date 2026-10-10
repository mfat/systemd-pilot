"""XDG Base Directory paths for this application.

See: https://specifications.freedesktop.org/basedir-spec/latest/
"""

from __future__ import annotations

import os
from pathlib import Path

# Directory name under the XDG locations (not the D-Bus app id).
APP_DIR = "systemd-pilot"


def xdg_config_home() -> Path:
    """``$XDG_CONFIG_HOME``, or ``~/.config`` when unset."""
    env = os.environ.get("XDG_CONFIG_HOME")
    return Path(env) if env else Path.home() / ".config"


def app_config_dir() -> Path:
    """Application config directory: ``$XDG_CONFIG_HOME/systemd-pilot``."""
    return xdg_config_home() / APP_DIR
