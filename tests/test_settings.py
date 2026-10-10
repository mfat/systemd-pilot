import json

import pytest

from systemdpilot.ui.settings import Settings


def test_view_mode_survives_restart_when_schema_lacks_key(tmp_path):
    path = tmp_path / "settings.json"
    settings = Settings(path)
    if settings._stored("view-mode"):
        pytest.skip("installed schema already includes view-mode")

    settings.set_string("view-mode", "advanced")
    assert json.loads(path.read_text())["view-mode"] == "advanced"
    assert Settings(path).get_string("view-mode") == "advanced"


def test_file_overrides_ignored_defaults(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"view-mode": "advanced", "unit-label-order": "description-name"}))
    settings = Settings(path)
    if settings._stored("view-mode"):
        pytest.skip("installed schema already includes view-mode")

    assert settings.get_string("view-mode") == "advanced"
    assert settings.get_string("unit-label-order") == "description-name"
