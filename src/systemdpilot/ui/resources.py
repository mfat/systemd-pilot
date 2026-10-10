"""Load UI templates and CSS from the GResource bundle, or from source files.

Installed copies register ``systemd-pilot.gresource``. When running from a
source checkout (``python3 run.py``) nothing is registered, and the files
next to this module are used instead, so no build step is needed.
"""

from __future__ import annotations

from pathlib import Path

from gi.repository import Gdk, Gio, GLib, Gtk

from .. import APP_ID, RESOURCE_PATH

_SOURCE_DIR = Path(__file__).resolve().parent


def _in_bundle(name: str) -> bool:
    try:
        Gio.resources_get_info(f"{RESOURCE_PATH}/{name}", Gio.ResourceLookupFlags.NONE)
        return True
    except GLib.Error:
        return False


def template(name: str) -> Gtk.Template:
    """Class decorator for the template ``ui/<name>``."""
    if _in_bundle(f"ui/{name}"):
        return Gtk.Template(resource_path=f"{RESOURCE_PATH}/ui/{name}")
    return Gtk.Template(filename=str(_SOURCE_DIR / name))


def app_icon(size: int) -> Gdk.Paintable | None:
    """The app icon from the bundle, or None without one.

    Looking it up by name would find an installed copy first, which may be older.
    """
    name = f"icons/scalable/apps/{APP_ID}.svg"
    if not _in_bundle(name):
        return None
    file = Gio.File.new_for_uri(f"resource://{RESOURCE_PATH}/{name}")
    return Gtk.IconPaintable.new_for_file(file, size, 2)  # drawn at twice the size, sharp on HiDPI


def load_css_from_source() -> None:
    """Load style.css and the app's own icons from the source tree when the bundle is not registered.

    With the bundle, Adw.Application loads them from its resource base path.
    """
    if _in_bundle("style.css"):
        return
    Gtk.IconTheme.get_for_display(Gdk.Display.get_default()).add_search_path(
        str(_SOURCE_DIR.parents[2] / "data" / "icons" / "actions")
    )
    provider = Gtk.CssProvider()
    provider.load_from_path(str(_SOURCE_DIR / "style.css"))
    Gtk.StyleContext.add_provider_for_display(
        Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
    )
