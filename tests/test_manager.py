import json
import subprocess

import pytest

from systemdpilot.core.errors import CommandError, InvalidUnitName, UnitExists
from systemdpilot.core.manager import SystemdManager
from systemdpilot.core.models import Scope, Unit, UnitAction
from systemdpilot.core.validation import normalize_service_name, validate_unit_name


@pytest.mark.parametrize("name", ["sshd.service", "getty@tty1.service", "a-b_c:d.service", "foo.timer"])
def test_valid_names(name):
    assert validate_unit_name(name) == name


@pytest.mark.parametrize(
    "name", ["", "../../etc/passwd.service", "a b.service", "foo", "-rf.service", "a;b.service", "x/y.service"]
)
def test_invalid_names(name):
    with pytest.raises(InvalidUnitName):
        validate_unit_name(name)


def test_normalize_service_name():
    assert normalize_service_name(" web ") == "web.service"
    assert normalize_service_name("web.timer") == "web.timer"


def test_list_units_merges_and_uses_scope(runner):
    runner.reply(
        "systemctl",
        "--no-pager",
        "--user",
        "list-units",
        stdout=json.dumps([{"unit": "a.service", "active": "active", "sub": "running"}]),
    )
    runner.reply(
        "systemctl",
        "--no-pager",
        "--user",
        "list-unit-files",
        stdout=json.dumps([{"unit_file": "a.service", "state": "enabled"}]),
    )
    manager = SystemdManager(runner)
    loaded = manager.list_units(Scope.USER, include_inactive=True)
    assert [(u.name, u.file_state) for u in loaded] == [("a.service", None)]
    units = manager.complete_units(loaded, Scope.USER, include_unloaded=True)
    assert [(u.name, u.file_state) for u in units] == [("a.service", "enabled")]
    assert all("--user" in c["argv"] for c in runner.calls)
    assert "--all" in runner.calls[0]["argv"]
    assert not any(c["privileged"] for c in runner.calls)


def test_list_units_falls_back_to_plain(runner):
    runner.reply(
        "systemctl",
        "--no-pager",
        "list-units",
        "--type=service",
        "--output=json",
        returncode=1,
        stderr="Unknown output 'json'.",
    )
    runner.reply(
        "systemctl",
        "--no-pager",
        "list-units",
        "--type=service",
        "--plain",
        stdout="a.service loaded active running A\n",
    )
    units = SystemdManager(runner).list_units()
    assert [u.name for u in units] == ["a.service"]


def test_complete_units_tolerates_failure(runner):
    runner.reply("systemctl", "--no-pager", "list-unit-files", returncode=1, stderr="boom")
    loaded = [Unit("a.service", active_state="active")]
    assert [u.file_state for u in SystemdManager(runner).complete_units(loaded)] == [""]


def test_list_units_failure_raises(runner):
    runner.reply("systemctl", returncode=1, stderr="Failed to connect to bus")
    with pytest.raises(CommandError, match="Failed to connect to bus"):
        SystemdManager(runner).list_units()


def test_control_system_is_privileged(runner):
    SystemdManager(runner).control("a.service", UnitAction.RESTART)
    call = runner.calls[0]
    assert call["argv"] == ["systemctl", "--no-pager", "restart", "--", "a.service"]
    assert call["privileged"]


def test_control_user_is_unprivileged(runner):
    SystemdManager(runner).control("a.service", UnitAction.STOP, Scope.USER)
    assert runner.calls[0]["argv"] == ["systemctl", "--no-pager", "--user", "stop", "--", "a.service"]
    assert not runner.calls[0]["privileged"]


def test_control_reports_failure(runner):
    runner.reply("systemctl", returncode=5, stderr="Unit a.service not found.")
    with pytest.raises(CommandError, match="not found"):
        SystemdManager(runner).control("a.service", UnitAction.START)


def test_control_rejects_bad_names(runner):
    with pytest.raises(InvalidUnitName):
        SystemdManager(runner).control("x; rm -rf /.service", UnitAction.START)
    assert runner.calls == []


def test_status_accepts_inactive_exit_code(runner):
    runner.reply("systemctl", returncode=3, stdout="○ a.service\n")
    assert SystemdManager(runner).status_text("a.service") == "○ a.service\n"


def test_logs_user_unit_and_permission_warning(runner):
    runner.reply(
        "journalctl",
        stdout=json.dumps({"MESSAGE": "hi"}) + "\n",
        stderr="Hint: You are currently not seeing messages from other users and the system.",
    )
    result = SystemdManager(runner).logs("a.service", Scope.USER, lines=10)
    assert runner.calls[0]["argv"][-2:] == ["--user-unit", "a.service"]
    assert [e.message for e in result.entries] == ["hi"]
    assert "systemd-journal" in result.warning


def _run_install_script(call, tmp_path, monkeypatch):
    """Execute the generated install script for real, with systemctl stubbed."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    (bindir / "systemctl").write_text('#!/bin/sh\necho "$@" > "$HOME/reload"\n')
    (bindir / "systemctl").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}:/usr/bin:/bin")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    return subprocess.run(call["argv"], input=call["input"], capture_output=True, text=True)


def test_create_user_unit_end_to_end(runner, tmp_path, monkeypatch):
    content = "[Service]\nExecStart=/bin/echo 'quoted' \"$HOME\"; `id`"
    path = SystemdManager(runner).create_unit("demo.service", content, Scope.USER)
    call = runner.calls[0]
    assert not call["privileged"]
    assert "demo.service" not in call["argv"][3]  # the name is an argument, not script text

    proc = _run_install_script(call, tmp_path, monkeypatch)
    assert proc.returncode == 0, proc.stderr
    unit = tmp_path / ".config/systemd/user/demo.service"
    assert unit.read_text() == content + "\n"
    assert oct(unit.stat().st_mode & 0o777) == "0o644"
    assert (tmp_path / "reload").read_text().strip() == "--user daemon-reload"
    assert path.endswith("/systemd/user/demo.service")

    # A second attempt refuses to overwrite.
    proc = _run_install_script(call, tmp_path, monkeypatch)
    assert proc.returncode == 17


def test_create_system_unit_is_privileged(runner):
    runner.reply("env", returncode=17, stderr="exists")
    with pytest.raises(UnitExists):
        SystemdManager(runner).create_unit("demo.service", "[Unit]\n")
    assert runner.calls[0]["privileged"]
    assert runner.calls[0]["argv"][-1] == "--system"
