"""Creating units: templates, names, ExecStart= editing, and the install script run for real."""

import configparser
import shutil
import subprocess

import pytest

from systemdpilot.core.errors import InvalidUnitName, PilotError, UnitActivationFailed, UnitExists
from systemdpilot.core.manager import SystemdManager
from systemdpilot.core.models import Scope
from systemdpilot.core.templates import (
    TEMPLATES,
    base_name,
    quote_exec_path,
    render,
    set_exec_start,
    unit_names,
)


def _template(template_id):
    return next(t for t in TEMPLATES if t.id == template_id)


def _parse(content):
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    parser.optionxform = str
    parser.read_string(content)
    return parser


# -- templates --------------------------------------------------------------


@pytest.mark.parametrize("template", TEMPLATES, ids=lambda t: t.id)
@pytest.mark.parametrize("user_scope", [False, True])
def test_templates_render_valid_unit_files(template, user_scope):
    files = render(template, user_scope)
    assert [unit_type for unit_type, _ in files] == list(template.types)
    assert template.activate in template.types
    for unit_type, content in files:
        assert "{" not in content and "}" not in content
        assert content.endswith("\n")
        parsed = _parse(content)
        assert parsed.has_section("Unit")
        if unit_type == "service":
            assert parsed.has_option("Service", "ExecStart")
        if user_scope:
            assert "User=" not in content and "multi-user.target" not in content


@pytest.mark.parametrize("template", TEMPLATES, ids=lambda t: t.id)
def test_only_the_activated_unit_is_installed(template):
    """A timer's or path's service is started by it, so it has no [Install] section of its own."""
    for unit_type, content in render(template, False):
        assert _parse(content).has_section("Install") == (unit_type == template.activate)


def test_wanted_by_follows_the_scope():
    service = dict(render(_template("simple"), False))["service"]
    assert _parse(service)["Install"]["WantedBy"] == "multi-user.target"
    service = dict(render(_template("simple"), True))["service"]
    assert _parse(service)["Install"]["WantedBy"] == "default.target"
    timer = dict(render(_template("timer"), True))["timer"]
    assert _parse(timer)["Install"]["WantedBy"] == "timers.target"


def test_system_service_template_offers_running_as_another_user():
    service = dict(render(_template("simple"), False))["service"]
    assert "#User=" in service
    assert "\n\n[Install]" in service
    assert "\n\n\n" not in dict(render(_template("simple"), True))["service"]


# -- names ------------------------------------------------------------------


@pytest.mark.parametrize("typed", ["backup", "backup.service", "backup.timer", "  backup.timer  "])
def test_unit_names_for_a_timer(typed):
    assert unit_names(_template("timer"), typed) == ["backup.service", "backup.timer"]


@pytest.mark.parametrize(
    "typed, expected",
    [
        ("web", ["web.service"]),
        ("web.service", ["web.service"]),
        ("worker@", ["worker@.service"]),
        ("org.example:app", ["org.example:app.service"]),
        (r"foo\x2dbar", [r"foo\x2dbar.service"]),
    ],
)
def test_unit_names_for_a_service(typed, expected):
    assert unit_names(_template("simple"), typed) == expected


def test_unit_names_for_a_path_unit():
    assert unit_names(_template("path"), "watch.path") == ["watch.service", "watch.path"]


@pytest.mark.parametrize("typed", ["web.socket", "backup.timer"])
def test_other_unit_types_are_refused(typed):
    with pytest.raises(InvalidUnitName, match="this template makes"):
        unit_names(_template("simple"), typed)


@pytest.mark.parametrize("typed", ["", "   ", "has space", "-dash", ".dot", "a/b", "x" * 250])
def test_bad_names_are_refused(typed):
    with pytest.raises(InvalidUnitName):
        unit_names(_template("timer"), typed)


def test_base_name():
    assert base_name(_template("timer"), "backup.timer") == "backup"
    assert base_name(_template("simple"), "backup") == "backup"


# -- ExecStart= -------------------------------------------------------------


@pytest.mark.parametrize(
    "path, expected",
    [
        ("/usr/bin/foo", "/usr/bin/foo"),
        ("/opt/My App/run", '"/opt/My App/run"'),
        ('/opt/a"b', r'"/opt/a\"b"'),
        ("/opt/a\\b", r'"/opt/a\\b"'),
        ("/opt/100%/run", "/opt/100%%/run"),
        ("/opt/$HOME/run", "/opt/$$HOME/run"),
    ],
)
def test_quote_exec_path(path, expected):
    assert quote_exec_path(path) == expected


def test_set_exec_start_replaces_the_first_line_only():
    content = "[Service]\nExecStart=/usr/bin/sleep infinity\nExecStart=/bin/second\n"
    assert set_exec_start(content, "/opt/run") == "[Service]\nExecStart=/opt/run\nExecStart=/bin/second\n"


def test_set_exec_start_ignores_similar_keys():
    content = "[Service]\nExecStartPre=/bin/pre\nExecStart=/bin/old\n"
    assert set_exec_start(content, "/opt/run") == "[Service]\nExecStartPre=/bin/pre\nExecStart=/opt/run\n"


def test_set_exec_start_adds_a_line_to_the_service_section():
    content = "[Unit]\nDescription=x\n\n[Service]\nType=simple\n\n[Install]\nWantedBy=multi-user.target\n"
    assert set_exec_start(content, "/opt/run") == (
        "[Unit]\nDescription=x\n\n[Service]\nType=simple\nExecStart=/opt/run\n\n[Install]\nWantedBy=multi-user.target\n"
    )
    assert set_exec_start("[Service]", "/opt/run") == "[Service]\nExecStart=/opt/run\n"


def test_set_exec_start_adds_a_service_section():
    assert set_exec_start("[Unit]\nDescription=x\n", "/opt/run") == (
        "[Unit]\nDescription=x\n\n[Service]\nExecStart=/opt/run\n"
    )
    assert set_exec_start("", "/opt/run") == "[Service]\nExecStart=/opt/run\n"


def test_set_exec_start_with_a_path_needing_quotes():
    result = set_exec_start("[Service]\nExecStart=/bin/true\n", "/opt/My App/run")
    assert result == '[Service]\nExecStart="/opt/My App/run"\n'


# -- the install script, run for real ---------------------------------------

# The script must work with whatever sh a host has.
SHELLS = [shell for shell in (["dash"], ["bash", "--posix"], ["busybox", "sh"]) if shutil.which(shell[0])]


def _systemctl_stub(bindir):
    """A systemctl that logs its calls and the unit directory at reload, and fails on request."""
    (bindir / "systemctl").write_text(
        """#!/bin/sh
echo "$*" >> "$STUB_LOG"
if [ "$2" = daemon-reload ]; then echo "files: $(ls -A "$STUB_DIR" | tr '\\n' ' ')" >> "$STUB_LOG"; fi
if [ "$2" = "${STUB_FAIL:-}" ]; then echo "Failed to $2 unit: denied" >&2; exit 1; fi
"""
    )
    (bindir / "systemctl").chmod(0o755)


@pytest.fixture(params=SHELLS, ids=lambda s: s[0])
def install(request, runner, tmp_path, monkeypatch):
    """Runs the manager's recorded install command with a real shell, in a fake home."""
    home = tmp_path / "home with space"
    home.mkdir()
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _systemctl_stub(bindir)
    unit_dir = home / ".config/systemd/user"
    log = tmp_path / "systemctl.log"
    monkeypatch.setenv("PATH", f"{bindir}:/usr/bin:/bin")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("STUB_LOG", str(log))
    monkeypatch.setenv("STUB_DIR", str(unit_dir))
    shell = request.param

    class Install:
        manager = SystemdManager(runner)
        dir = unit_dir

        @staticmethod
        def run(fail=""):
            call = runner.calls[-1]
            argv = shell + call["argv"][1:]  # the shell in place of "sh"
            monkeypatch.setenv("STUB_FAIL", fail)
            return subprocess.run(argv, input=call["input"], capture_output=True, text=True)

        @staticmethod
        def calls():
            return log.read_text().splitlines() if log.exists() else []

        @staticmethod
        def files():
            return sorted(p.name for p in unit_dir.iterdir()) if unit_dir.exists() else []

    return Install


def test_one_file_is_installed_and_reloaded(install):
    content = "[Service]\nExecStart=/bin/echo 'quoted' \"$HOME\"; `id` $(id) \\\n  ünïcødé\n-leading dash"
    paths = install.manager.create_units([("demo.service", content)], Scope.USER)
    assert paths[0].endswith("/systemd/user/demo.service")
    proc = install.run()
    assert proc.returncode == 0, proc.stderr
    unit = install.dir / "demo.service"
    assert unit.read_text() == content + "\n"
    assert oct(unit.stat().st_mode & 0o777) == "0o644"
    assert install.calls() == ["--user daemon-reload", "files: demo.service "]


def test_several_files_are_installed_together(install):
    files = render(_template("timer"), True)
    names = unit_names(_template("timer"), "backup")
    install.manager.create_units(list(zip(names, [c for _, c in files], strict=True)), Scope.USER)
    proc = install.run()
    assert proc.returncode == 0, proc.stderr
    assert install.files() == ["backup.service", "backup.timer"]
    assert (install.dir / "backup.timer").read_text() == files[1][1]
    # Reloaded once, with both files in place and no temporary file left.
    assert install.calls() == ["--user daemon-reload", "files: backup.service backup.timer "]


def test_unusual_names_are_installed(install):
    names = ["foo\\x2dbar@.service", "org.example:app.service"]
    install.manager.create_units([(n, "[Unit]\n") for n in names], Scope.USER)
    proc = install.run()
    assert proc.returncode == 0, proc.stderr
    assert install.files() == sorted(names)


def test_an_existing_file_stops_everything(install):
    install.dir.mkdir(parents=True)
    (install.dir / "backup.timer").write_text("old timer\n")
    install.manager.create_units([("backup.service", "[Unit]\n"), ("backup.timer", "[Unit]\n")], Scope.USER)
    proc = install.run()
    assert proc.returncode == 17
    assert proc.stderr.strip().endswith("/systemd/user/backup.timer")
    # The service put in place first is taken back; the existing timer is left alone.
    assert install.files() == ["backup.timer"]
    assert (install.dir / "backup.timer").read_text() == "old timer\n"
    assert install.calls() == []  # systemd was not touched


def test_overwrite_replaces_existing_files(install):
    install.dir.mkdir(parents=True)
    (install.dir / "backup.service").write_text("old\n")
    install.manager.create_units(
        [("backup.service", "[Unit]\nDescription=new"), ("backup.timer", "[Timer]\n")], Scope.USER, overwrite=True
    )
    proc = install.run()
    assert proc.returncode == 0, proc.stderr
    assert (install.dir / "backup.service").read_text() == "[Unit]\nDescription=new\n"
    assert install.files() == ["backup.service", "backup.timer"]


def test_enable_and_start_follow_the_reload(install):
    install.manager.create_units(
        [("backup.service", "[Unit]\n"), ("backup.timer", "[Timer]\n")],
        Scope.USER,
        enable=True,
        start=True,
        activate="backup.timer",
    )
    proc = install.run()
    assert proc.returncode == 0, proc.stderr
    assert install.calls() == [
        "--user daemon-reload",
        "files: backup.service backup.timer ",
        "--user enable -- backup.timer",
        "--user start -- backup.timer",
    ]


def test_start_only(install):
    install.manager.create_unit("demo.service", "[Unit]\n", Scope.USER, start=True)
    assert install.run().returncode == 0
    assert install.calls()[-1] == "--user start -- demo.service"
    assert not any("enable" in line for line in install.calls())


@pytest.mark.parametrize("action, code", [("enable", 18), ("start", 19)])
def test_activation_failure_leaves_the_files_in_place(install, action, code):
    install.manager.create_unit("demo.service", "[Unit]\n", Scope.USER, enable=True, start=True)
    proc = install.run(fail=action)
    assert proc.returncode == code
    assert "denied" in proc.stderr
    assert install.files() == ["demo.service"]


def test_short_input_installs_nothing(install):
    install.manager.create_units([("a.service", "[Unit]\n"), ("b.service", "[Unit]\n")], Scope.USER)
    call = install.manager.runner.calls[-1]
    call["input"] = call["input"].splitlines(keepends=True)[0]  # the second file's content is missing
    proc = install.run()
    assert proc.returncode != 0
    assert install.files() == []
    assert install.calls() == []


def test_failed_reload_is_reported(install):
    install.manager.create_unit("demo.service", "[Unit]\n", Scope.USER)
    proc = install.run(fail="daemon-reload")
    assert proc.returncode not in (0, 17, 18, 19)


# -- how the manager reports results ----------------------------------------


def test_names_are_arguments_not_script_text(runner):
    SystemdManager(runner).create_units([("demo.service", "[Unit]\n"), ("demo.timer", "[Timer]\n")])
    argv = runner.calls[0]["argv"]
    assert "demo" not in argv[2]
    assert argv[-2:] == ["demo.service", "demo.timer"]


def test_system_units_are_privileged(runner):
    SystemdManager(runner).create_units([("demo.service", "[Unit]\n")])
    call = runner.calls[0]
    assert call["privileged"]
    assert call["argv"][4] == "--system"
    assert 'dir="/etc/systemd/system"' in call["argv"][2]


def test_user_units_are_not_privileged(runner):
    SystemdManager(runner).create_units([("demo.service", "[Unit]\n")], Scope.USER)
    assert not runner.calls[0]["privileged"]
    assert runner.calls[0]["argv"][4] == "--user"


def test_exists_names_the_existing_file(runner):
    runner.reply("sh", returncode=17, stderr="/etc/systemd/system/demo.timer\n")
    with pytest.raises(UnitExists) as info:
        SystemdManager(runner).create_units([("demo.service", "[Unit]\n"), ("demo.timer", "[Timer]\n")])
    assert info.value.path == "/etc/systemd/system/demo.timer"


@pytest.mark.parametrize("code, action", [(18, "enabled"), (19, "started")])
def test_activation_failure_is_reported(runner, code, action):
    runner.reply("sh", returncode=code, stderr="Access denied\n")
    with pytest.raises(UnitActivationFailed) as info:
        SystemdManager(runner).create_unit("demo.service", "[Unit]\n", enable=True)
    assert info.value.action == action
    assert info.value.paths == ["/etc/systemd/system/demo.service"]
    assert "Access denied" in str(info.value)


def test_other_failures_raise(runner):
    runner.reply("sh", returncode=1, stderr="Failed to reload daemon\n")
    with pytest.raises(PilotError, match="Failed to reload"):
        SystemdManager(runner).create_unit("demo.service", "[Unit]\n")


@pytest.mark.parametrize(
    "files, kwargs",
    [
        ([], {}),
        ([("demo.service", "  \n")], {}),
        ([("demo.service", "[Unit]\n"), ("demo.service", "[Unit]\n")], {}),
        ([("demo.service", "[Unit]\n")], {"enable": True, "activate": "other.service"}),
        ([("demo.service", "[Unit]\n")], {"start": True}),
        ([("bad name.service", "[Unit]\n")], {}),
    ],
)
def test_bad_requests_run_nothing(runner, files, kwargs):
    with pytest.raises(PilotError):
        SystemdManager(runner).create_units(files, **kwargs)
    assert runner.calls == []
