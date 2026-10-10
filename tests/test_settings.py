import json

import pytest

from systemdpilot.core.paths import app_config_dir
from systemdpilot.ui.settings import Settings


def test_label_order_survives_restart_when_schema_lacks_key(tmp_path):
    path = tmp_path / "settings.json"
    settings = Settings(path)
    if settings._stored("unit-label-order"):
        pytest.skip("installed schema already includes unit-label-order")

    settings.set_string("unit-label-order", "description-name")
    assert json.loads(path.read_text())["unit-label-order"] == "description-name"
    assert Settings(path).get_string("unit-label-order") == "description-name"


def test_file_overrides_defaults_and_drops_unknown_keys(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"view-mode": "advanced", "unit-label-order": "description-name"}))
    settings = Settings(path)
    if settings._stored("unit-label-order"):
        pytest.skip("installed schema already includes unit-label-order")

    assert settings.get_string("unit-label-order") == "description-name"
    # The Simple/Advanced setting is gone: an old file forgets it.
    assert "view-mode" not in json.loads(path.read_text())


def test_default_settings_path_is_under_xdg_config(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    settings = Settings()
    assert settings._path == app_config_dir() / "settings.json"
    assert settings._path == tmp_path / "systemd-pilot" / "settings.json"
