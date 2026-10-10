"""Application entry point."""

from __future__ import annotations

import logging
import sys
from gettext import gettext as _

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

from . import APP_ID, RESOURCE_PATH, WEBSITE  # noqa: E402
from .core.hosts import HostStore  # noqa: E402
from .core.known_hosts import KnownHosts  # noqa: E402
from .core.paths import app_config_dir  # noqa: E402
from .core.secrets import LibsecretStore, MemorySecretStore  # noqa: E402
from .core.session import Sessions  # noqa: E402
from .ui.resources import load_css_from_source  # noqa: E402
from .ui.settings import Settings  # noqa: E402
from .ui.window import Window  # noqa: E402

log = logging.getLogger(__name__)

COLOR_SCHEMES = {
    "default": Adw.ColorScheme.DEFAULT,
    "light": Adw.ColorScheme.FORCE_LIGHT,
    "dark": Adw.ColorScheme.FORCE_DARK,
}


class Application(Adw.Application):
    def __init__(self, version: str):
        super().__init__(application_id=APP_ID, resource_base_path=RESOURCE_PATH)
        self.version = version
        self.settings = Settings()
        self.sessions: Sessions | None = None
        self.add_main_option(
            "verbose", ord("v"), GLib.OptionFlags.NONE, GLib.OptionArg.NONE, _("Print debug messages"), None
        )

    def do_handle_local_options(self, options):
        verbose = options.contains("verbose")
        logging.basicConfig(
            level=logging.DEBUG if verbose else logging.WARNING,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        )
        if not verbose:
            logging.getLogger("paramiko").setLevel(logging.WARNING)
        return -1

    def do_startup(self):
        Adw.Application.do_startup(self)
        load_css_from_source()

        config_dir = app_config_dir()
        try:
            secrets = LibsecretStore()
        except (ValueError, ImportError) as e:
            log.warning("libsecret is unavailable, passwords will not be saved: %s", e)
            secrets = MemorySecretStore()
        hosts = HostStore(config_dir, secrets)
        hosts.load()
        self.sessions = Sessions(hosts, KnownHosts(config_dir / "known_hosts"))

        self._add_action("quit", lambda *_: self.quit(), ["<primary>q"])
        self._add_action("about", self._on_about)

        scheme = self.settings.get_string("color-scheme")
        action = Gio.SimpleAction.new_stateful("color-scheme", GLib.VariantType.new("s"), GLib.Variant("s", scheme))
        action.connect("change-state", self._on_color_scheme)
        self.add_action(action)
        self._apply_color_scheme(scheme)

        self.set_accels_for_action("win.refresh", ["F5", "<primary>r"])
        self.set_accels_for_action("win.search", ["<primary>f"])
        self.set_accels_for_action("win.create-unit", ["<primary>n"])
        self.set_accels_for_action("win.show-inactive", ["<primary>h"])
        self.set_accels_for_action("window.close", ["<primary>w"])

    def do_activate(self):
        # The journal window may be the active one; it belongs to the main window.
        window = next((w for w in self.get_windows() if isinstance(w, Window)), None)
        if not window:
            window = Window(application=self, sessions=self.sessions, settings=self.settings)
        window.present()

    def do_shutdown(self):
        if self.sessions:
            self.sessions.close_all()
        Adw.Application.do_shutdown(self)

    def _add_action(self, name, callback, accels=None):
        action = Gio.SimpleAction.new(name, None)
        action.connect("activate", callback)
        self.add_action(action)
        if accels:
            self.set_accels_for_action(f"app.{name}", accels)

    def _on_color_scheme(self, action, value):
        scheme = value.get_string()
        if scheme not in COLOR_SCHEMES:
            return
        action.set_state(value)
        self.settings.set_string("color-scheme", scheme)
        self._apply_color_scheme(scheme)

    def _apply_color_scheme(self, scheme):
        Adw.StyleManager.get_default().set_color_scheme(COLOR_SCHEMES.get(scheme, Adw.ColorScheme.DEFAULT))

    def _on_about(self, *_args):
        about = Adw.AboutDialog(
            application_name=_("systemd Pilot"),
            application_icon=APP_ID,
            developer_name="mFat",
            version=self.version,
            website=WEBSITE,
            issue_url=f"{WEBSITE}/issues",
            license_type=Gtk.License.GPL_3_0,
            copyright="© 2024–2026 mFat",
            developers=["mFat https://github.com/mfat", "HattonLe https://github.com/HattonLe"],
        )
        # Translators: replace with your name(s), one per line
        credits = _("translator-credits")
        if credits != "translator-credits":
            about.set_translator_credits(credits)
        about.present(self.props.active_window)


def main(version: str) -> int:
    return Application(version).run(sys.argv)
