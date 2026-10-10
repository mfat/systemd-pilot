from pathlib import Path

from systemdpilot.core.paths import app_config_dir, xdg_config_home


def test_xdg_config_home_respects_env(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    assert xdg_config_home() == tmp_path / "cfg"
    assert app_config_dir() == tmp_path / "cfg" / "systemd-pilot"


def test_xdg_config_home_default(monkeypatch):
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    assert xdg_config_home() == Path.home() / ".config"
    assert app_config_dir() == Path.home() / ".config" / "systemd-pilot"
