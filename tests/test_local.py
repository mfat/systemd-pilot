import subprocess

import pytest

from systemdpilot.core import local
from systemdpilot.core.errors import AuthenticationCancelled, AuthenticationFailed, CommandError
from systemdpilot.core.local import LocalRunner


@pytest.mark.parametrize(
    ("sandboxed", "privileged", "expected"),
    [
        (False, False, ["systemctl", "status"]),
        (False, True, ["pkexec", "systemctl", "status"]),
        (True, False, ["flatpak-spawn", "--host", "--watch-bus", "systemctl", "status"]),
        # pkexec must run on the host, inside flatpak-spawn, not the other way round.
        (True, True, ["flatpak-spawn", "--host", "--watch-bus", "pkexec", "systemctl", "status"]),
    ],
)
def test_build_argv(sandboxed, privileged, expected):
    argv = LocalRunner(sandboxed=sandboxed).build_argv(["systemctl", "status"], privileged)
    assert [a for a in argv if not a.startswith("--env=")] == expected
    if sandboxed:
        assert "--env=SYSTEMD_COLORS=0" in argv


class FakeRun:
    def __init__(self, returncode=0, stdout="", stderr="", exc=None):
        self.result = subprocess.CompletedProcess([], returncode, stdout, stderr)
        self.exc = exc
        self.kwargs = None

    def __call__(self, cmd, **kwargs):
        self.cmd, self.kwargs = cmd, kwargs
        if self.exc:
            raise self.exc
        return self.result


def test_run_passes_input_and_env(monkeypatch):
    fake = FakeRun(stdout="ok")
    monkeypatch.setattr(local.subprocess, "run", fake)
    result = LocalRunner(sandboxed=False).run(["cat"], input="data")
    assert result.ok and result.stdout == "ok"
    assert fake.kwargs["input"] == "data"
    assert fake.kwargs["env"]["SYSTEMD_COLORS"] == "0"


def test_privileged_has_no_timeout(monkeypatch):
    fake = FakeRun()
    monkeypatch.setattr(local.subprocess, "run", fake)
    LocalRunner(sandboxed=False).run(["true"], privileged=True, timeout=5)
    assert fake.kwargs["timeout"] is None  # a person is answering a polkit dialog


def test_pkexec_dismissed(monkeypatch):
    monkeypatch.setattr(local.subprocess, "run", FakeRun(returncode=126))
    with pytest.raises(AuthenticationCancelled):
        LocalRunner(sandboxed=False).run(["systemctl", "start", "a.service"], privileged=True)


def test_pkexec_not_authorized(monkeypatch):
    monkeypatch.setattr(
        local.subprocess, "run", FakeRun(returncode=127, stderr="Error executing command: Not authorized")
    )
    with pytest.raises(AuthenticationFailed):
        LocalRunner(sandboxed=False).run(["systemctl", "start", "a.service"], privileged=True)


def test_unprivileged_127_is_a_normal_result(monkeypatch):
    monkeypatch.setattr(local.subprocess, "run", FakeRun(returncode=127, stderr="not found"))
    assert LocalRunner(sandboxed=False).run(["nope"]).returncode == 127


def test_missing_binary(monkeypatch):
    monkeypatch.setattr(local.subprocess, "run", FakeRun(exc=FileNotFoundError(2, "nope", "flatpak-spawn")))
    with pytest.raises(CommandError, match="command not found"):
        LocalRunner(sandboxed=True).run(["systemctl"])


def test_timeout(monkeypatch):
    monkeypatch.setattr(local.subprocess, "run", FakeRun(exc=subprocess.TimeoutExpired("x", 1)))
    with pytest.raises(CommandError, match="timed out"):
        LocalRunner(sandboxed=False).run(["sleep", "9"])
