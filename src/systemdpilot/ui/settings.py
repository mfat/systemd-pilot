"""GSettings, with a small file fallback when the schema is missing or outdated."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from gi.repository import Gio, GLib

from .. import APP_ID

log = logging.getLogger(__name__)

_DEFAULTS = {
    "window-width": GLib.Variant("i", 1000),
    "window-height": GLib.Variant("i", 680),
    "window-maximized": GLib.Variant("b", False),
    "show-inactive": GLib.Variant("b", False),
    "color-scheme": GLib.Variant("s", "default"),
    "view-mode": GLib.Variant("s", "simple"),
    "unit-label-order": GLib.Variant("s", "name-description"),
}


def _json_value(variant: GLib.Variant):
    if variant.is_of_type(GLib.VariantType("i")):
        return variant.get_int32()
    if variant.is_of_type(GLib.VariantType("b")):
        return variant.get_boolean()
    if variant.is_of_type(GLib.VariantType("s")):
        return variant.get_string()
    raise TypeError(f"unsupported settings type: {variant.get_type_string()}")


def _variant(default: GLib.Variant, raw):
    if default.is_of_type(GLib.VariantType("i")):
        return GLib.Variant("i", int(raw))
    if default.is_of_type(GLib.VariantType("b")):
        return GLib.Variant("b", bool(raw))
    if default.is_of_type(GLib.VariantType("s")):
        return GLib.Variant("s", str(raw))
    raise TypeError(f"unsupported settings type: {default.get_type_string()}")


class Settings:
    def __init__(self, path: Path | None = None):
        source = Gio.SettingsSchemaSource.get_default()
        self._gsettings = Gio.Settings.new(APP_ID) if source and source.lookup(APP_ID, True) else None
        self._memory = dict(_DEFAULTS)
        self._path = path or Path(GLib.get_user_config_dir()) / "systemd-pilot" / "settings.json"
        self._file: dict = {}
        self._load_file()

    def _stored(self, key) -> bool:
        # An older installed schema may lack newer keys; GSettings aborts on unknown keys.
        return self._gsettings is not None and self._gsettings.props.settings_schema.has_key(key)

    def _load_file(self) -> None:
        if not self._path.is_file():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as e:
            log.warning("Could not read %s: %s", self._path, e)
            return
        if not isinstance(data, dict):
            return
        remaining: dict = {}
        changed = False
        for key, raw in data.items():
            default = _DEFAULTS.get(key)
            if default is None:
                continue
            try:
                value = _variant(default, raw)
            except (TypeError, ValueError):
                continue
            if self._stored(key):
                # Schema caught up: move the preference into GSettings once.
                self._gsettings.set_value(key, value)
                changed = True
            else:
                self._memory[key] = value
                remaining[key] = _json_value(value)
        self._file = remaining
        if changed or remaining != data:
            self._write_file()

    def _write_file(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            if self._file:
                self._path.write_text(json.dumps(self._file, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            elif self._path.exists():
                self._path.unlink()
        except OSError as e:
            log.warning("Could not write %s: %s", self._path, e)

    def _get(self, key):
        return self._gsettings.get_value(key) if self._stored(key) else self._memory[key]

    def _set(self, key, value):
        if self._stored(key):
            self._gsettings.set_value(key, value)
            return
        self._memory[key] = value
        self._file[key] = _json_value(value)
        self._write_file()

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
