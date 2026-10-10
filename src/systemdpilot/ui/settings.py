"""GSettings, with in-memory defaults when the schema is not installed."""

from __future__ import annotations

from gi.repository import Gio, GLib

from .. import APP_ID

_DEFAULTS = {
    "window-width": GLib.Variant("i", 1000),
    "window-height": GLib.Variant("i", 680),
    "window-maximized": GLib.Variant("b", False),
    "show-inactive": GLib.Variant("b", False),
    "color-scheme": GLib.Variant("s", "default"),
    "view-mode": GLib.Variant("s", "simple"),
}


class Settings:
    def __init__(self):
        source = Gio.SettingsSchemaSource.get_default()
        self._gsettings = Gio.Settings.new(APP_ID) if source and source.lookup(APP_ID, True) else None
        self._memory = dict(_DEFAULTS)

    def _stored(self, key) -> bool:
        # An older installed schema may lack newer keys; GSettings aborts on unknown keys.
        return self._gsettings is not None and self._gsettings.props.settings_schema.has_key(key)

    def _get(self, key):
        return self._gsettings.get_value(key) if self._stored(key) else self._memory[key]

    def _set(self, key, value):
        if self._stored(key):
            self._gsettings.set_value(key, value)
        else:
            self._memory[key] = value

    def get_int(self, key):
        return self._get(key).get_int32()

    def get_boolean(self, key):
        return self._get(key).get_boolean()

    def get_string(self, key):
        return self._get(key).get_string()

    def set_int(self, key, value):
        self._set(key, GLib.Variant("i", value))

    def set_boolean(self, key, value):
        self._set(key, GLib.Variant("b", value))

    def set_string(self, key, value):
        self._set(key, GLib.Variant("s", value))
