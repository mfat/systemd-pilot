#!/usr/bin/env bash
# Smoke-test an AppDir before packaging/appimage/build-appimage.sh packs it.
#
#   packaging/appimage/smoke-test.sh APPDIR [SMOKE_UI_PY]
#
# Runs under AppRun's environment (minus the final exec), with env -i, so the
# bundle has to stand on its own. Once for the host C library and once forced
# onto the bundled copy -- the build host is never older than itself, so the
# bundled-libc run is the only coverage of what users on older distros get.
#
# With a display (e.g. xvfb-run) and SMOKE_UI_PY, also runs the UI smoke test
# against the bundled interpreter and the installed app under AppDir.
set -euo pipefail

log() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
die() { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

APPDIR=${1:-}
SMOKE_UI=${2:-}
[ -n "$APPDIR" ] || die "usage: $0 APPDIR [SMOKE_UI_PY]"
[ -d "$APPDIR" ] || die "AppDir not found: $APPDIR"
APPDIR=$(cd "$APPDIR" && pwd)
[ -x "$APPDIR/AppRun" ] || die "no executable AppRun in $APPDIR"
[ -z "$SMOKE_UI" ] || [ -f "$SMOKE_UI" ] || die "smoke UI script not found: $SMOKE_UI"
[ -z "$SMOKE_UI" ] || SMOKE_UI=$(cd "$(dirname "$SMOKE_UI")" && pwd)/$(basename "$SMOKE_UI")

BUILD_TMP=$(dirname "$APPDIR")
# The environment comes from AppRun itself, with only its final exec line
# stripped, so this tests what users actually get and the two can never drift.
grep -v '^exec ' "$APPDIR/AppRun" > "$BUILD_TMP/apprun-env.sh"

run_under_apprun() {
    # $1 = host|bundled; remaining args are the python command.
    local libc=$1
    shift
    # Fresh HOME: nothing from the build user's home can leak in.
    local smoke_home
    smoke_home=$(mktemp -d "${TMPDIR:-/tmp}/systemd-pilot-appimage-smoke.XXXXXX")
    env -i \
        HOME="$smoke_home" \
        PATH=/usr/bin:/bin \
        APPDIR="$APPDIR" \
        SYSTEMD_PILOT_APPIMAGE_LIBC="$libc" \
        ${DISPLAY:+DISPLAY="$DISPLAY"} \
        ${WAYLAND_DISPLAY:+WAYLAND_DISPLAY="$WAYLAND_DISPLAY"} \
        ${XAUTHORITY:+XAUTHORITY="$XAUTHORITY"} \
        ${XDG_RUNTIME_DIR:+XDG_RUNTIME_DIR="$XDG_RUNTIME_DIR"} \
        ${DBUS_SESSION_BUS_ADDRESS:+DBUS_SESSION_BUS_ADDRESS="$DBUS_SESSION_BUS_ADDRESS"} \
        bash -c '. "$1"; shift; exec "$SYSTEMD_PILOT_PYTHON" -s "$@"' \
        _ "$BUILD_TMP/apprun-env.sh" "$@"
}

smoke_integrity() {
    local libc=$1
    log "Smoke test on the $libc C library"
    run_under_apprun "$libc" - "$APPDIR" "$libc" <<'PY'
import os
import subprocess
import sys

appdir = sys.argv[1]
libc_mode = sys.argv[2]
assert sys.executable.startswith(appdir), sys.executable

with open("/proc/self/maps", encoding="utf-8") as maps:
    libc_paths = {line.split()[-1] for line in maps if line.rstrip().endswith("/libc.so.6")}
bundled_libc = os.path.join(appdir, "usr", "lib", "libc", "libc.so.6")
if libc_mode == "bundled":
    assert libc_paths == {bundled_libc}, libc_paths
    assert sys.executable.endswith("/python3-bundled-libc"), sys.executable
else:
    assert bundled_libc not in libc_paths, libc_paths

child = subprocess.run(
    [sys.executable, "-c",
     "import gi; gi.require_version('Gtk', '4.0'); from gi.repository import Gtk"],
    capture_output=True, text=True,
)
assert child.returncode == 0, child.stderr

for variable, expected in (
    ("GI_TYPELIB_PATH", "Gtk-4.0.typelib"),
    ("GSETTINGS_SCHEMA_DIR", "gschemas.compiled"),
    ("GIO_MODULE_DIR", None),
):
    value = os.environ.get(variable)
    assert value and value.startswith(appdir), "%s=%r" % (variable, value)
    assert os.path.isdir(value), "%s=%r is not a directory" % (variable, value)
    if expected:
        assert os.path.exists(os.path.join(value, expected)), \
            "%s has no %s" % (variable, expected)

import gi

for namespace, version in (
    ("Gtk", "4.0"),
    ("Gdk", "4.0"),
    ("Adw", "1"),
    ("GtkSource", "5"),
    ("Secret", "1"),
    ("GLibUnix", "2.0"),
    ("PangoFT2", "1.0"),
    ("GdkPixbuf", "2.0"),
):
    gi.require_version(namespace, version)
from gi.repository import (  # noqa: F401
    Adw, Gdk, GdkPixbuf, Gio, GLib, Gtk, GtkSource, Secret,
)

import cairo  # noqa: F401
import paramiko  # noqa: F401

pkgdatadir = os.path.join(appdir, "usr", "share", "systemd-pilot")
sys.path.insert(0, pkgdatadir)
import systemdpilot  # noqa: E402

assert systemdpilot.__file__.startswith(appdir), systemdpilot.__file__
gresource = os.path.join(pkgdatadir, "systemd-pilot.gresource")
assert os.path.isfile(gresource), gresource
Gio.Resource.load(gresource)._register()

formats = {fmt.get_name() for fmt in GdkPixbuf.Pixbuf.get_formats()}
assert "svg" in formats, sorted(formats)

print("smoke test ok: integrity (%s libc)" % libc_mode)
PY
}

smoke_ui() {
    local libc=$1
    log "UI smoke test on the $libc C library"
    run_under_apprun "$libc" - "$APPDIR" "$SMOKE_UI" <<'PY'
import os
import runpy
import sys

appdir, smoke_ui = sys.argv[1], sys.argv[2]
pkgdatadir = os.path.join(appdir, "usr", "share", "systemd-pilot")
sys.path.insert(0, pkgdatadir)
gresource = os.path.join(pkgdatadir, "systemd-pilot.gresource")
os.environ["SYSTEMD_PILOT_RESOURCE"] = gresource

# Templates live only in the GResource (Meson excludes the .ui files from
# install). Register before importing the UI package.
from gi.repository import Gio

Gio.Resource.load(gresource)._register()

# Libsecret's constructor only imports the typelib; the hang is on each
# password_*_sync call when the session bus has a portal but no keyring
# (~25s each), which eats the UI smoke timeout. Force the memory store.
import systemdpilot.main as app_main


class _NoKeyring:
    def __init__(self):
        raise ValueError("keyring disabled for AppImage smoke")


app_main.LibsecretStore = _NoKeyring
runpy.run_path(smoke_ui, run_name="__main__")
PY
}

smoke_integrity host
smoke_integrity bundled

if [ -n "$SMOKE_UI" ]; then
    if [ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]; then
        smoke_ui host
        smoke_ui bundled
    else
        log "No display; skipping UI smoke test (run under xvfb-run for full coverage)"
    fi
fi
