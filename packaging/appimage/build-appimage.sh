#!/usr/bin/env bash
# Build a self-contained systemd Pilot AppImage.
#
#   packaging/appimage/build-appimage.sh [VERSION] [OUTDIR]
#
# VERSION defaults to the version in meson.build, OUTDIR to dist/.
#
# Adapted from sshPilot's AppImage build. The AppImage bundles the GTK 4 stack
# (GTK, libadwaita, GtkSourceView, libsecret), their typelibs, a CPython
# interpreter, PyGObject and paramiko with its dependencies. Not bundled:
# anything host-coupled (see packaging/appimage/excludelist), and the systemd
# tools themselves -- the app drives the host's systemctl, journalctl and
# pkexec, as it does in every other package. glibc is bundled too, but off to
# the side, for hosts older than the build host only (see 8b).
#
# What makes the result relocatable:
#
#   * every bundled ELF gets an $ORIGIN RPATH, so nothing needs
#     LD_LIBRARY_PATH and nothing the app spawns inherits our libraries;
#   * the bundled interpreter sits in the usual $prefix/bin + $prefix/lib
#     layout, so it finds its standard library with no PYTHONHOME;
#   * the launcher Meson generates is rewritten to find the app, its
#     GResource and the bundled Python modules relative to its own location,
#     instead of the /usr baked in at install time.
#
# Build host: Ubuntu 24.04, the oldest release carrying libadwaita >= 1.5.
# Hosts with an older glibc (Ubuntu 22.04) run on the bundled copy (step 8b
# and AppRun); the host libraries in the excludelist -- GL, X11, Wayland,
# fonts -- are what still sets a floor there.
set -euo pipefail

log() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
die() { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
ROOT=$(cd "$SCRIPT_DIR/../.." && pwd)

APP_ID=io.github.mfat.systemdpilot
ARCH=$(uname -m)
PYTHON_BIN=${PYTHON_BIN:-/usr/bin/python3}
BUILD_DIR=${BUILD_DIR:-$ROOT/build/appimage}
APPDIR=$BUILD_DIR/AppDir
OUTDIR=${2:-$ROOT/dist}

VERSION=${1:-}
if [ -z "$VERSION" ]; then
    VERSION=$(sed -nE "s/^\s*version:\s*'([^']+)'.*/\1/p" "$ROOT/meson.build" | head -1)
    [ -n "$VERSION" ] || die "no version in meson.build"
fi
VERSION=${VERSION#v}

[ -x "$PYTHON_BIN" ] || die "no Python interpreter at $PYTHON_BIN (set PYTHON_BIN)"
for tool in meson ninja patchelf glib-compile-schemas ldconfig; do
    command -v "$tool" >/dev/null || die "$tool is required but not installed"
done

# The bundled interpreter, PyGObject and every compiled wheel have to be the
# same build: PYTHON_BIN is what the AppImage ships.
PY_VER=$("$PYTHON_BIN" -c 'import sys; print("%d.%d" % sys.version_info[:2])')
PY_REAL=$("$PYTHON_BIN" -c 'import os, sys; print(os.path.realpath(sys.executable))')
PY_STDLIB=$("$PYTHON_BIN" -c 'import sysconfig; print(sysconfig.get_paths()["stdlib"])')

log "Building systemd Pilot $VERSION AppImage for $ARCH (python $PY_VER from $PY_REAL)"

rm -rf "$BUILD_DIR"
mkdir -p "$BUILD_DIR" "$OUTDIR"

# --------------------------------------------------------------------------
# 1. Install the app the same way every distro package does.
# --------------------------------------------------------------------------
log "Installing with Meson"
meson setup "$BUILD_DIR/meson" "$ROOT" --prefix=/usr --buildtype=release -Dtests=false
meson install -C "$BUILD_DIR/meson" --destdir "$APPDIR"

PKGDATADIR=$APPDIR/usr/share/systemd-pilot
[ -d "$PKGDATADIR/systemdpilot" ] || die "Meson did not install the app under $PKGDATADIR"
[ -f "$PKGDATADIR/systemd-pilot.gresource" ] || die "no GResource bundle in $PKGDATADIR"

LIBDIR="$APPDIR/usr/lib"
# Debian's CPython looks here relative to its prefix; the bundled interpreter
# therefore finds PyGObject and paramiko with no PYTHONPATH. The app itself
# lives under share/systemd-pilot (Meson's pkgdatadir), which the launcher adds.
DEPSDIR="$LIBDIR/python3/dist-packages"
TYPELIBDIR="$LIBDIR/girepository-1.0"
PIXBUF_DIR="$LIBDIR/gdk-pixbuf-2.0/2.10.0"
mkdir -p "$LIBDIR" "$DEPSDIR" "$TYPELIBDIR" "$PIXBUF_DIR/loaders" "$LIBDIR/gio/modules"

# --------------------------------------------------------------------------
# 2. The interpreter and its standard library.
# --------------------------------------------------------------------------
log "Bundling CPython $PY_VER"
install -Dm755 "$PY_REAL" "$APPDIR/usr/bin/python$PY_VER"
ln -sf "python$PY_VER" "$APPDIR/usr/bin/python3"
cp -a "$PY_STDLIB" "$LIBDIR/"

# Nothing in the app imports these, and config-*/ carries a static libpython.
STDLIB_COPY="$LIBDIR/$(basename "$PY_STDLIB")"
rm -rf "$STDLIB_COPY/test" "$STDLIB_COPY/idlelib" "$STDLIB_COPY/tkinter" \
       "$STDLIB_COPY/turtledemo" "$STDLIB_COPY/ensurepip" "$STDLIB_COPY/lib2to3" \
       "$STDLIB_COPY"/config-*
find "$STDLIB_COPY" -name __pycache__ -type d -prune -exec rm -rf {} +

# --------------------------------------------------------------------------
# 3. Python dependencies: PyGObject (and pycairo, which it uses for cairo
#    interop) from the distro, since they are C extensions against the system
#    GObject stack; paramiko and its dependencies from PyPI.
# --------------------------------------------------------------------------
log "Bundling PyGObject"
for pkg in gi cairo; do
    if pkg_dir=$("$PYTHON_BIN" -c "import $pkg, os; print(os.path.dirname($pkg.__file__))" 2>/dev/null); then
        cp -a "$pkg_dir" "$DEPSDIR/"
    elif [ "$pkg" = gi ]; then
        die "$PYTHON_BIN cannot import gi (install python3-gi)"
    fi
done

log "Installing paramiko and its dependencies"
"$PYTHON_BIN" -m pip install \
    --disable-pip-version-check \
    --no-compile \
    --no-warn-script-location \
    --only-binary=:all: \
    --target "$DEPSDIR" \
    paramiko
# pip --target leaves the console scripts of the dependencies behind; the app
# has no use for them and they carry absolute shebangs.
rm -rf "$DEPSDIR/bin"
find "$DEPSDIR" -name __pycache__ -type d -prune -exec rm -rf {} +

# --------------------------------------------------------------------------
# 4. Make the Meson install relocatable.
# --------------------------------------------------------------------------
log "Rewriting the launcher for a relocatable prefix"
"$PYTHON_BIN" - "$APPDIR/usr/bin/systemd-pilot" <<'PY'
"""Replace the Meson launcher, which names /usr paths, with a relocatable one.

Inside an AppImage /usr is the *host's*: a systemd-pilot installed from a
distro package would win over the bundled one. The new launcher finds the app,
its GResource, translations and the bundled Python modules relative to its own
location, which changes on every run.
"""
import os
import re
import stat
import sys

path = sys.argv[1]
# Meson installs the launcher mode 555; make it writable before rewriting.
os.chmod(path, os.stat(path).st_mode | stat.S_IWUSR)
version = re.search(r'^VERSION = "([^"]+)"', open(path, encoding="utf-8").read(), re.M).group(1)

LAUNCHER = '''#!/usr/bin/env python3
"""systemd Pilot launcher (AppImage build): paths relative to this file."""

import gettext
import locale
import os
import signal
import sys

VERSION = %(version)r
prefix = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
pkgdatadir = os.path.join(prefix, "share", "systemd-pilot")
localedir = os.path.join(prefix, "share", "locale")

# Position 0: the bundled app must win even if the host has it installed too.
# PyGObject and paramiko live in usr/lib/python3/dist-packages and are found
# by the bundled interpreter on their own.
sys.path.insert(0, pkgdatadir)
signal.signal(signal.SIGINT, signal.SIG_DFL)

try:
    locale.setlocale(locale.LC_ALL, "")
    locale.bindtextdomain("systemd-pilot", localedir)
    locale.textdomain("systemd-pilot")
except (AttributeError, locale.Error):
    pass
gettext.bindtextdomain("systemd-pilot", localedir)
gettext.textdomain("systemd-pilot")

if __name__ == "__main__":
    from gi.repository import Gio

    Gio.Resource.load(os.path.join(pkgdatadir, "systemd-pilot.gresource"))._register()

    from systemdpilot import main

    sys.exit(main.main(VERSION))
'''

with open(path, "w", encoding="utf-8") as handle:
    handle.write(LAUNCHER % {"version": version})
os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
PY

# --------------------------------------------------------------------------
# 5. GObject introspection typelibs, and the libraries they name.
# --------------------------------------------------------------------------
log "Bundling typelibs"
# Every namespace the app calls gi.require_version() for, plus whatever those
# pull in transitively.
"$PYTHON_BIN" - > "$BUILD_DIR/typelibs.txt" <<'PY'
import sys

import gi

# Newer hosts ship only GLib's GIRepository-3.0; the Ubuntu 24.04 floor ships
# gobject-introspection's 2.0. The two APIs differ slightly below.
try:
    gi.require_version("GIRepository", "3.0")
except ValueError:
    gi.require_version("GIRepository", "2.0")
from gi.repository import GIRepository

WANTED = [
    ("Gtk", "4.0"),
    ("Gdk", "4.0"),
    ("Gsk", "4.0"),
    ("Adw", "1"),
    ("GtkSource", "5"),
    ("Secret", "1"),
    ("Gio", "2.0"),
    ("GLib", "2.0"),
    ("GLibUnix", "2.0"),
    ("GObject", "2.0"),
    ("GdkPixbuf", "2.0"),
    ("Pango", "1.0"),
    ("PangoCairo", "1.0"),
    ("PangoFT2", "1.0"),
    ("cairo", "1.0"),
    ("Graphene", "1.0"),
]


def lookup(namespace, version):
    """Return (typelib path, shared libraries, immediate dependencies)."""
    if gi.get_required_version("GIRepository") == "3.0":
        # A fresh repository per namespace: reusing one makes
        # get_shared_libraries() return the first namespace's libraries for
        # every later one (seen with GLib 2.88 / PyGObject 3.56).
        repo = GIRepository.Repository.new()
        repo.require(namespace, version, 0)
        libraries = repo.get_shared_libraries(namespace)
    else:
        repo = GIRepository.Repository.get_default()
        repo.require(namespace, version, 0)
        libraries = (repo.get_shared_library(namespace) or "").split(",")
    return (repo.get_typelib_path(namespace),
            [lib for lib in libraries if lib],
            repo.get_immediate_dependencies(namespace))


seen = set()
queue = list(WANTED)
while queue:
    namespace, version = queue.pop(0)
    if namespace in seen:
        continue
    seen.add(namespace)
    try:
        typelib, libraries, dependencies = lookup(namespace, version)
    except Exception as exc:  # a namespace this build's GLib does not ship
        print("SKIP\t%s-%s\t%s" % (namespace, version, exc), file=sys.stderr)
        continue
    print("%s\t%s" % (typelib, ",".join(libraries)))
    for dep in dependencies:
        dep_ns, _, dep_ver = dep.partition("-")
        queue.append((dep_ns, dep_ver))
PY

typelib_libs=()
while IFS=$'\t' read -r typelib shared; do
    [ -n "$typelib" ] || continue
    install -Dm644 "$typelib" "$TYPELIBDIR/$(basename "$typelib")"
    IFS=',' read -ra sonames <<<"$shared"
    for soname in "${sonames[@]}"; do
        if [ -n "$soname" ]; then
            typelib_libs+=("$soname")
        fi
    done
done < "$BUILD_DIR/typelibs.txt"
[ "${#typelib_libs[@]}" -gt 0 ] || die "no typelibs were resolved"

# --------------------------------------------------------------------------
# 6. Loadable modules GTK/GLib open by path rather than by DT_NEEDED.
# --------------------------------------------------------------------------
# Read once, not piped per lookup: awk exits at the first match, and a cache
# listing larger than the pipe buffer then kills ldconfig with SIGPIPE, which
# pipefail turns into a build failure.
LDCONFIG_CACHE=$(ldconfig -p)
resolve_soname() {
    awk -v name="$1" '$1 == name { print $NF; exit }' <<<"$LDCONFIG_CACHE"
}

SYS_LIBDIR=$(dirname "$(resolve_soname libglib-2.0.so.0)")
[ -d "$SYS_LIBDIR" ] || die "could not locate the system library directory"

log "Bundling gdk-pixbuf loaders and GIO modules"
cp -a "$SYS_LIBDIR/gdk-pixbuf-2.0/2.10.0/loaders/." "$PIXBUF_DIR/loaders/"
# dconf is how GSettings reaches the host's desktop settings -- without it
# there is no system dark-mode preference to follow, and the app's own
# settings would not persist.
for module in "$SYS_LIBDIR"/gio/modules/*.so; do
    [ -e "$module" ] && cp -a "$module" "$LIBDIR/gio/modules/"
done

# --------------------------------------------------------------------------
# 7. Shared libraries: the transitive closure of everything bundled so far,
#    minus what has to come from the host.
# --------------------------------------------------------------------------
log "Collecting shared libraries"
declare -A EXCLUDED=()
while read -r entry; do
    entry=${entry%%#*}
    entry=$(printf '%s' "$entry" | tr -d '[:space:]')
    [ -n "$entry" ] && EXCLUDED[$entry]=1
done < "$SCRIPT_DIR/excludelist"

declare -A COLLECTED=()
queue=("$APPDIR/usr/bin/python$PY_VER")
while IFS= read -r -d '' elf; do
    queue+=("$elf")
done < <(find "$DEPSDIR" "$STDLIB_COPY/lib-dynload" "$PIXBUF_DIR/loaders" \
              "$LIBDIR/gio/modules" \( -name '*.so' -o -name '*.so.*' \) -print0 2>/dev/null)
for soname in "${typelib_libs[@]}"; do
    # A typelib naming a host library (HarfBuzz names libharfbuzz.so.0) is no
    # reason to bundle it: GI dlopen()s it by soname, so the host copy loads.
    [ -n "${EXCLUDED[$soname]:-}" ] && continue
    path=$(resolve_soname "$soname")
    [ -n "$path" ] || die "typelib names $soname but ldconfig cannot find it"
    [ -n "${COLLECTED[$soname]:-}" ] && continue
    COLLECTED[$soname]=1
    install -Dm644 "$(readlink -f "$path")" "$LIBDIR/$soname"
    queue+=("$LIBDIR/$soname")
done

while [ "${#queue[@]}" -gt 0 ]; do
    current=${queue[0]}
    queue=("${queue[@]:1}")
    while read -r name arrow path _; do
        [ "$arrow" = "=>" ] || continue
        case "$path" in /*) ;; *) continue ;; esac
        [ -n "${EXCLUDED[$name]:-}" ] && continue
        [ -n "${COLLECTED[$name]:-}" ] && continue
        # Wheels carry their own copies of some libraries (e.g. in *.libs/);
        # those are already in the bundle and keep their own RPATH.
        case "$path" in "$APPDIR"/*) continue ;; esac
        COLLECTED[$name]=1
        # Copied under its SONAME: that is the name every DT_NEEDED uses.
        install -Dm644 "$(readlink -f "$path")" "$LIBDIR/$name"
        queue+=("$LIBDIR/$name")
    done < <(ldd "$current" 2>/dev/null || true)
done
echo "bundled ${#COLLECTED[@]} shared libraries"

# --------------------------------------------------------------------------
# 8. RPATHs. This is what lets AppRun skip LD_LIBRARY_PATH entirely -- and so
#    what keeps systemctl, journalctl and pkexec running against the host's
#    libraries when the app starts them.
# --------------------------------------------------------------------------
log "Setting \$ORIGIN RPATHs"
set_rpath() {
    local elf=$1 rel
    rel=$(realpath --relative-to="$(dirname "$elf")" "$LIBDIR")
    if [ "$rel" = "." ]; then
        patchelf --set-rpath '$ORIGIN' "$elf" 2>/dev/null || true
    else
        patchelf --set-rpath "\$ORIGIN/$rel" "$elf" 2>/dev/null || true
    fi
}

set_rpath "$APPDIR/usr/bin/python$PY_VER"
# Wheels (cryptography, bcrypt, pynacl, cffi) come self-contained from PyPI
# with their own RPATHs; only the distro-provided modules need ours.
while IFS= read -r -d '' elf; do
    set_rpath "$elf"
done < <(find "$LIBDIR" -maxdepth 1 \( -name '*.so' -o -name '*.so.*' \) -print0)
while IFS= read -r -d '' elf; do
    set_rpath "$elf"
done < <(find "$DEPSDIR/gi" "$DEPSDIR/cairo" "$STDLIB_COPY/lib-dynload" "$PIXBUF_DIR/loaders" \
              "$LIBDIR/gio/modules" -name '*.so' -print0 2>/dev/null)

# --------------------------------------------------------------------------
# 8b. The C library, for hosts older than this build host. Done after the
#     RPATH pass on purpose: patchelf must never touch the loader or libc.
# --------------------------------------------------------------------------
log "Bundling the C library for hosts older than the build host"
# Everything above was linked against this host's glibc, so a host with an
# older one cannot run it natively. AppRun compares the two versions: on an
# older host it starts the interpreter through the bundled loader instead,
# which then takes glibc -- and the C++ runtime, which is just as new -- from
# here. On anything as new as the build host nothing below is used.
#
# The directory is reached only through the loader's --library-path, never an
# RPATH: a bundled libc.so.6 found by the *host's* loader would be a mixed
# glibc, which crashes. Host programs the app starts (systemctl, pkexec) are
# exec'd normally and never see it.
LIBC_DIR=$LIBDIR/libc
mkdir -p "$LIBC_DIR"
LOADER=$(patchelf --print-interpreter "$APPDIR/usr/bin/python$PY_VER")
LOADER_NAME=$(basename "$LOADER")
install -Dm755 "$(readlink -f "$LOADER")" "$LIBC_DIR/$LOADER_NAME"
# HarfBuzz rides along: it is in the excludelist, but GTK and Pango here need
# HarfBuzz >= 7 and the hosts that get this directory can be older (Ubuntu
# 22.04 has 2.7). Newer hosts keep their own.
for soname in libc.so.6 libm.so.6 libmvec.so.1 libpthread.so.0 libdl.so.2 \
              librt.so.1 libresolv.so.2 libutil.so.1 libanl.so.1 \
              libnss_files.so.2 libnss_dns.so.2 \
              libstdc++.so.6 libgcc_s.so.1 \
              libharfbuzz.so.0; do
    path=$(resolve_soname "$soname")
    if [ -z "$path" ]; then
        # Merged into libc.so.6 on some glibc versions; libc itself is not optional.
        [ "$soname" != libc.so.6 ] || die "ldconfig cannot find libc.so.6"
        continue
    fi
    install -Dm644 "$(readlink -f "$path")" "$LIBC_DIR/$soname"
done
LIBC_VERSION=$(getconf GNU_LIBC_VERSION | awk '{ print $2 }')
[ -n "$LIBC_VERSION" ] || die "getconf cannot report the glibc version"
printf '%s\n' "$LIBC_VERSION" > "$LIBC_DIR/VERSION"
echo "bundled glibc $LIBC_VERSION ($LOADER_NAME)"

# The loader cannot be named in PT_INTERP -- that has to be an absolute path,
# and the mount point changes on every run -- so the interpreter gets a
# wrapper that runs it through the loader. --argv0 keeps the wrapper's own path
# in argv[0]: that is what Python reports as sys.executable, and what it uses
# to find its standard library.
cat > "$APPDIR/usr/bin/python3-bundled-libc" <<EOF
#!/bin/sh
# Runs usr/bin/python$PY_VER on the C library bundled in usr/lib/libc.
# AppRun picks this only on hosts whose glibc is older than $LIBC_VERSION.
here=\$(dirname "\$0")
exec "\$here/../lib/libc/$LOADER_NAME" --library-path "\$here/../lib/libc" \\
    --argv0 "\$0" "\$here/python$PY_VER" "\$@"
EOF
chmod 755 "$APPDIR/usr/bin/python3-bundled-libc"

# --------------------------------------------------------------------------
# 9. Data GTK looks up by path: schemas, icon themes, pixbuf loader cache.
# --------------------------------------------------------------------------
log "Bundling GSettings schemas and icon themes"
# The app's own schema is already in the AppDir (Meson installed it); the
# host's are added for the GTK and desktop settings GTK reads.
mkdir -p "$APPDIR/usr/share/glib-2.0/schemas"
cp -a /usr/share/glib-2.0/schemas/*.xml "$APPDIR/usr/share/glib-2.0/schemas/" 2>/dev/null || true
cp -a /usr/share/glib-2.0/schemas/*.gschema.override "$APPDIR/usr/share/glib-2.0/schemas/" 2>/dev/null || true
glib-compile-schemas "$APPDIR/usr/share/glib-2.0/schemas" >/dev/null

mkdir -p "$APPDIR/usr/share/icons"
[ -d /usr/share/icons/Adwaita ] || die "adwaita-icon-theme is not installed"
cp -a /usr/share/icons/Adwaita "$APPDIR/usr/share/icons/"
install -Dm644 /usr/share/icons/hicolor/index.theme \
    "$APPDIR/usr/share/icons/hicolor/index.theme"

# The cache has to name the loaders by absolute path, and the mount point is
# only known at run time -- AppRun expands @APPDIR@ into a per-user copy.
QUERY_LOADERS=$SYS_LIBDIR/gdk-pixbuf-2.0/gdk-pixbuf-query-loaders
[ -x "$QUERY_LOADERS" ] || QUERY_LOADERS=$(command -v gdk-pixbuf-query-loaders || true)
[ -x "$QUERY_LOADERS" ] || die "gdk-pixbuf-query-loaders not found"
"$QUERY_LOADERS" "$PIXBUF_DIR/loaders/"*.so > "$PIXBUF_DIR/loaders.cache.in"
sed -i "s|$APPDIR|@APPDIR@|g" "$PIXBUF_DIR/loaders.cache.in"

# --------------------------------------------------------------------------
# 10. AppDir metadata.
# --------------------------------------------------------------------------
log "Assembling AppDir metadata"
install -Dm755 "$SCRIPT_DIR/AppRun" "$APPDIR/AppRun"
install -Dm644 "$APPDIR/usr/share/applications/$APP_ID.desktop" "$APPDIR/$APP_ID.desktop"
install -Dm644 "$APPDIR/usr/share/icons/hicolor/scalable/apps/$APP_ID.svg" "$APPDIR/$APP_ID.svg"
ln -sf "$APP_ID.svg" "$APPDIR/.DirIcon"
if command -v desktop-file-validate >/dev/null; then
    desktop-file-validate "$APPDIR/$APP_ID.desktop"
fi

# --------------------------------------------------------------------------
# 11. Smoke-test the bundle before packaging it.
# --------------------------------------------------------------------------
"$SCRIPT_DIR/smoke-test.sh" "$APPDIR" "$ROOT/tests/smoke_ui.py"

# --------------------------------------------------------------------------
# 12. Pack it.
# --------------------------------------------------------------------------
log "Building the AppImage"
APPIMAGETOOL=${APPIMAGETOOL:-$BUILD_DIR/appimagetool}
if [ ! -x "$APPIMAGETOOL" ]; then
    # AppImage/appimagetool rather than the archived AppImageKit: the runtime
    # it embeds is static and needs no libfuse2, which recent distros no
    # longer install by default.
    wget -q -O "$APPIMAGETOOL" \
        "https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-$ARCH.AppImage"
    chmod +x "$APPIMAGETOOL"
fi

OUTPUT="$OUTDIR/systemd-pilot-$VERSION-$ARCH.AppImage"
# Runners have no FUSE; extracting the tool is the supported way around it.
APPIMAGE_EXTRACT_AND_RUN=1 ARCH="$ARCH" "$APPIMAGETOOL" "$APPDIR" "$OUTPUT"

log "Built $OUTPUT ($(du -h "$OUTPUT" | cut -f1))"
