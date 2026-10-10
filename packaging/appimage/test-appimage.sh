#!/usr/bin/env bash
# Test a finished AppImage on the system this runs on -- in CI, Ubuntu 22.04.
#
#   dbus-run-session -- xvfb-run -a packaging/appimage/test-appimage.sh APPIMAGE [SMOKE_UI_PY]
#
# Unlike smoke-test.sh, which forces each C library on the build host, this
# leaves every choice to AppRun (C library, renderer), so it tests exactly
# what a user of this system gets:
#
#   1. the UI smoke test, run by the bundled interpreter under AppRun's
#      environment, with GTK criticals fatal;
#   2. the app itself, started through the AppImage, which has to keep
#      running for 30 seconds.
#
# Needs a display and a session bus, hence the wrappers above.
set -euo pipefail

log() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
die() { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

APPIMAGE=${1:-}
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
SMOKE_UI=${2:-$ROOT/tests/smoke_ui.py}
[ -f "$APPIMAGE" ] || die "usage: $0 APPIMAGE [SMOKE_UI_PY]"
[ -f "$SMOKE_UI" ] || die "smoke UI script not found: $SMOKE_UI"
[ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ] || die "no display; run under xvfb-run"
APPIMAGE=$(readlink -f "$APPIMAGE")

WORK=$(mktemp -d "${TMPDIR:-/tmp}/systemd-pilot-appimage-test.XXXXXX")
trap 'rm -rf "$WORK"' EXIT
cd "$WORK"

log "Host: $(. /etc/os-release && echo "$PRETTY_NAME"), $(getconf GNU_LIBC_VERSION)"
# Extracting rather than mounting: containers and CI runners have no FUSE.
"$APPIMAGE" --appimage-extract >/dev/null
APPDIR=$WORK/squashfs-root
grep -v '^exec ' "$APPDIR/AppRun" > "$WORK/apprun-env.sh"
mkdir -p "$WORK/home"

apprun_env() {
    env -i \
        HOME="$WORK/home" \
        PATH=/usr/bin:/bin \
        APPDIR="$APPDIR" \
        G_DEBUG=fatal-criticals \
        ${DISPLAY:+DISPLAY="$DISPLAY"} \
        ${WAYLAND_DISPLAY:+WAYLAND_DISPLAY="$WAYLAND_DISPLAY"} \
        ${XAUTHORITY:+XAUTHORITY="$XAUTHORITY"} \
        ${XDG_RUNTIME_DIR:+XDG_RUNTIME_DIR="$XDG_RUNTIME_DIR"} \
        ${DBUS_SESSION_BUS_ADDRESS:+DBUS_SESSION_BUS_ADDRESS="$DBUS_SESSION_BUS_ADDRESS"} \
        "$@"
}

log "AppRun's choices"
apprun_env sh -c '. "$1"; echo "python:   $SYSTEMD_PILOT_PYTHON"; echo "renderer: ${GSK_RENDERER:-default}"' \
    _ "$WORK/apprun-env.sh"

log "UI smoke test"
pkgdatadir=$APPDIR/usr/share/systemd-pilot
apprun_env SYSTEMD_PILOT_RESOURCE="$pkgdatadir/systemd-pilot.gresource" \
    sh -c '. "$1"; shift; exec "$SYSTEMD_PILOT_PYTHON" -s "$@"' _ "$WORK/apprun-env.sh" \
    -c 'import runpy, sys; sys.path.insert(0, sys.argv[1]); runpy.run_path(sys.argv[2], run_name="__main__")' \
    "$pkgdatadir" "$SMOKE_UI"

log "Starting the app through the AppImage for 30 seconds"
status=0
apprun_env APPIMAGE_EXTRACT_AND_RUN=1 timeout 30 "$APPIMAGE" > "$WORK/run.log" 2>&1 || status=$?
if [ "$status" -ne 124 ]; then
    cat "$WORK/run.log"
    die "the app exited with status $status instead of running until stopped"
fi
echo "still running after 30 seconds: ok"
