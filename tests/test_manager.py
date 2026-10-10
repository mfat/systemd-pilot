import json
import subprocess

import pytest

from systemdpilot.core.errors import CommandError, InvalidUnitName, PilotError, UnitExists
from systemdpilot.core.manager import SystemdManager
from systemdpilot.core.models import Scope, Unit, UnitAction
from systemdpilot.core.runner import CommandResult, CommandRunner
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
    units = manager.complete_units(loaded, include_unloaded=True)
    assert all(u.scope is Scope.USER for u in units)
    assert [(u.name, u.file_state) for u in units] == [("a.service", "enabled")]
    assert all("--user" in c["argv"] for c in runner.calls)
    assert "--all" in runner.calls[0]["argv"]
    assert not any(c["privileged"] for c in runner.calls)


def _fake_systemctl(tmp_path, monkeypatch, user_ok=True):
    """A systemctl on PATH that answers list-units for each scope."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    script = """#!/bin/sh
case "$*" in
  *--system*) echo '[{"unit":"a.service","active":"active"},{"unit":"dbus.service","active":"active"}]' ;;
  *--user*) %s ;;
esac
""" % (
        """echo '[{"unit":"dbus.service","active":"active"}]'"""
        if user_ok
        else 'echo "Failed to connect to bus: No medium found" >&2; exit 1'
    )
    (bindir / "systemctl").write_text(script)
    (bindir / "systemctl").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}:/usr/bin:/bin")


class _ShellRunner(CommandRunner):
    """Runs commands for real, so the combined script is exercised."""

    def __init__(self):
        self.calls = []

    def run(self, argv, *, input=None, privileged=False, timeout=60):
        self.calls.append(list(argv))
        done = subprocess.run(list(argv), input=input, capture_output=True, text=True)
        return CommandResult(tuple(argv), done.returncode, done.stdout, done.stderr)


def test_list_services_lists_both_scopes_in_one_command(tmp_path, monkeypatch):
    _fake_systemctl(tmp_path, monkeypatch)
    runner = _ShellRunner()
    units = SystemdManager(runner).list_services()
    assert [u.key for u in units] == [
        (Scope.SYSTEM, "a.service"),
        (Scope.SYSTEM, "dbus.service"),
        (Scope.USER, "dbus.service"),
    ]
    assert len(runner.calls) == 1


def test_list_services_without_a_user_instance(tmp_path, monkeypatch):
    _fake_systemctl(tmp_path, monkeypatch, user_ok=False)
    units = SystemdManager(_ShellRunner()).list_services()
    assert [u.key for u in units] == [(Scope.SYSTEM, "a.service"), (Scope.SYSTEM, "dbus.service")]


def test_list_services_system_failure_raises(runner):
    runner.reply("sh", stdout="", stderr="sh: not found", returncode=127)
    with pytest.raises(CommandError):
        SystemdManager(runner).list_services()


def test_runtime_is_asked_per_scope(runner):
    runner.reply("systemctl", "--no-pager", "--user", "show", stdout="Id=dbus.service\nMainPID=9\n")
    runner.reply("systemctl", "--no-pager", "show", stdout="Id=dbus.service\nMainPID=1\n")
    units = [
        Unit("dbus.service", active_state="active", sub_state="running"),
        Unit("dbus.service", active_state="active", sub_state="running", scope=Scope.USER),
    ]
    system, user = SystemdManager(runner).add_runtime(units)
    assert (system.main_pid, user.main_pid) == (1, 9)


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


def test_create_user_unit_overwrite(runner, tmp_path, monkeypatch):
    manager = SystemdManager(runner)
    manager.create_unit("demo.service", "[Unit]\nDescription=old", Scope.USER)
    assert _run_install_script(runner.calls[-1], tmp_path, monkeypatch).returncode == 0

    manager.create_unit("demo.service", "[Unit]\nDescription=new", Scope.USER, overwrite=True)
    proc = _run_install_script(runner.calls[-1], tmp_path, monkeypatch)
    assert proc.returncode == 0, proc.stderr
    unit_dir = tmp_path / ".config/systemd/user"
    assert (unit_dir / "demo.service").read_text() == "[Unit]\nDescription=new\n"
    assert [p.name for p in unit_dir.iterdir()] == ["demo.service"]  # no temp files left behind


def test_complete_units_adds_runtime(runner):
    runner.reply(
        "systemctl",
        "--no-pager",
        "show",
        stdout=(
            "Id=a.service\nMainPID=42\nMemoryCurrent=1048576\nActiveEnterTimestamp=@1700000000\n"
            "InactiveEnterTimestamp=@1600000000\n\n"
            "Id=b.service\nMainPID=0\nMemoryCurrent=[not set]\nActiveEnterTimestamp=@1700000000\n"
            "InactiveEnterTimestamp=@1700000100\n"
        ),
    )
    loaded = [
        Unit("a.service", active_state="active", sub_state="running"),
        Unit("b.service", active_state="failed", sub_state="failed"),
        Unit("c.service", active_state="inactive", sub_state="dead"),
    ]
    a, b, c = SystemdManager(runner).complete_units(loaded)
    assert (a.main_pid, a.memory, a.since.timestamp()) == (42, 1048576, 1700000000)
    assert (b.main_pid, b.memory, b.since.timestamp()) == (0, None, 1700000100)
    assert c == Unit("c.service", active_state="inactive", sub_state="dead", file_state="")
    show = next(call["argv"] for call in runner.calls if "show" in call["argv"])
    assert "--timestamp=unix" in show
    assert show[-2:] == ["a.service", "b.service"]  # inactive units have nothing to show


@pytest.mark.parametrize(
    "active_state, prop",
    [
        ("active", "ActiveEnterTimestamp"),
        ("reloading", "ActiveEnterTimestamp"),
        ("failed", "InactiveEnterTimestamp"),
        ("activating", "InactiveExitTimestamp"),
        ("deactivating", "ActiveExitTimestamp"),
    ],
)
def test_since_follows_systemctl_status(runner, active_state, prop):
    stamps = {
        "ActiveEnterTimestamp": 1,
        "ActiveExitTimestamp": 2,
        "InactiveEnterTimestamp": 3,
        "InactiveExitTimestamp": 4,
    }
    runner.reply(
        "systemctl",
        "--no-pager",
        "show",
        stdout="Id=a.service\n" + "".join(f"{name}=@{value}\n" for name, value in stamps.items()),
    )
    (unit,) = SystemdManager(runner).add_runtime([Unit("a.service", active_state=active_state, sub_state="x")])
    assert unit.since.timestamp() == stamps[prop]


def test_attach_file_states_skips_runtime(runner):
    runner.reply(
        "systemctl",
        "--no-pager",
        "list-unit-files",
        stdout='[{"unit_file":"a.service","state":"enabled"},{"unit_file":"c.service","state":"disabled"}]',
    )
    loaded = [Unit("a.service", active_state="active", sub_state="running")]
    (unit,) = SystemdManager(runner).attach_file_states(loaded)
    assert unit.file_state == "enabled" and unit.since is None and unit.main_pid == 0
    assert not any("show" in call["argv"] for call in runner.calls)


def test_attach_file_states_caches_unit_files(runner):
    runner.reply(
        "systemctl",
        "--no-pager",
        "list-unit-files",
        stdout='[{"unit_file":"a.service","state":"enabled"}]',
    )
    manager = SystemdManager(runner)
    loaded = [Unit("a.service", active_state="active", sub_state="running")]
    manager.attach_file_states(loaded)
    manager.attach_file_states(loaded)
    assert sum(1 for call in runner.calls if "list-unit-files" in call["argv"]) == 1
    manager.invalidate_unit_files()
    runner.replies.clear()
    runner.reply(
        "systemctl",
        "--no-pager",
        "list-unit-files",
        stdout='[{"unit_file":"a.service","state":"disabled"}]',
    )
    (unit,) = manager.attach_file_states(loaded)
    assert unit.file_state == "disabled"
    assert sum(1 for call in runner.calls if "list-unit-files" in call["argv"]) == 2


def test_runtime_without_unix_timestamps(runner):
    runner.reply(
        "systemctl",
        "--no-pager",
        "show",
        "--property=Id,MainPID,MemoryCurrent,ActiveEnterTimestamp,ActiveExitTimestamp,InactiveEnterTimestamp,InactiveExitTimestamp",
        "--timestamp=unix",
        returncode=1,
        stderr="unrecognized option '--timestamp=unix'",
    )
    runner.reply("systemctl", "--no-pager", "show", stdout="Id=a.service\nMainPID=7\n")
    (unit,) = SystemdManager(runner).add_runtime([Unit("a.service", active_state="active", sub_state="running")])
    assert unit.main_pid == 7 and unit.since is None


def test_journal_arguments(runner):
    runner.reply("journalctl", stdout=json.dumps({"MESSAGE": "hi", "PRIORITY": "4"}))
    result = SystemdManager(runner).journal(since="24 hours ago", boot=-1, kernel=True, lines=10)
    argv = runner.calls[0]["argv"]
    assert {"--reverse", "--lines=10", "--boot=-1", "--since=24 hours ago", "--dmesg", "--all"} <= set(argv)
    assert [e.message for e in result.entries] == ["hi"]
    assert not runner.calls[0]["privileged"]


def test_journal_custom_range(runner):
    SystemdManager(runner).journal(since="2026-10-10 14:00:00", until="2026-10-11 09:30:00")
    argv = runner.calls[0]["argv"]
    assert {"--since=2026-10-10 14:00:00", "--until=2026-10-11 09:30:00"} <= set(argv)


def test_journal_failure_raises(runner):
    runner.reply("journalctl", returncode=1, stderr="Failed to open journal")
    with pytest.raises(CommandError):
        SystemdManager(runner).journal()


def test_boot_id(runner):
    runner.reply("cat", stdout="e8a1f04c-1111-2222-3333-444455556666\n")
    assert SystemdManager(runner).boot_id() == "e8a1f04c111122223333444455556666"


def test_journal_can_be_read_as_root(runner):
    SystemdManager(runner).journal(privileged=True)
    assert runner.calls[0]["privileged"]


@pytest.mark.parametrize(
    ("groups", "entry", "expected"),
    [
        ("me wheel systemd-journal", "", "full"),
        ("me wheel", "systemd-journal:x:190:other,me", "pending"),
        ("me wheel", "systemd-journal:x:190:", "missing"),
        ("me wheel", "", "missing"),
    ],
)
def test_journal_access(runner, groups, entry, expected):
    runner.reply("id", "-Gn", stdout=groups + "\n")
    runner.reply("id", "-un", stdout="me\n")
    runner.reply("getent", stdout=entry + "\n")
    assert SystemdManager(runner).journal_access() == expected


def test_grant_journal_access_adds_user_to_group(runner):
    runner.reply("id", "-un", stdout="me\n")
    SystemdManager(runner).grant_journal_access()
    call = runner.calls[-1]
    assert call["argv"] == ["gpasswd", "-a", "me", "systemd-journal"] and call["privileged"]


def test_grant_journal_access_rejects_odd_user_names(runner):
    runner.reply("id", "-un", stdout="me; rm -rf /\n")
    with pytest.raises(PilotError):
        SystemdManager(runner).grant_journal_access()
    assert not any(c["privileged"] for c in runner.calls)


def test_unit_logs_can_be_read_as_root(runner):
    SystemdManager(runner).logs("a.service", privileged=True)
    assert runner.calls[0]["privileged"]
