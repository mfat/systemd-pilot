import threading

import paramiko
import pytest

from systemdpilot.core.errors import (
    AuthenticationFailed,
    AuthenticationRequired,
    ConnectionCancelled,
    HostKeyMismatch,
    HostKeyUnknown,
)
from systemdpilot.core.known_hosts import KnownHosts, fingerprint
from systemdpilot.core.models import AuthMethod, Host
from systemdpilot.core.ssh import SSHRunner


class ScriptedRunner(SSHRunner):
    """SSHRunner with the network replaced by a fake remote shell."""

    def __init__(self, uid="1000", nopasswd=False, password="secret"):
        super().__init__(Host("h", "h.example", "me"), KnownHosts(path=None, system_path=None))
        self.uid, self.nopasswd, self.password = uid, nopasswd, password
        self.executed: list[tuple[str, str | None]] = []

    def _exec(self, command, stdin, timeout):
        self.executed.append((command, stdin))
        if command == "id -u":
            return 0, self.uid + "\n", ""
        if command == "sudo -n true":
            return (0, "", "") if self.nopasswd else (1, "", "sudo: a password is required\n")
        if command.startswith("sudo -k -S"):
            given = (stdin or "").split("\n", 1)[0]
            if given != self.password:
                return 1, "", "Sorry, try again.\nsudo: 3 incorrect password attempts\n"
        return 0, "ok", ""


def test_unprivileged_commands_are_quoted():
    r = ScriptedRunner()
    r.run(["systemctl", "status", "--", "a b'c.service"])
    cmd = r.executed[-1][0]
    assert cmd.endswith("systemctl status -- 'a b'\"'\"'c.service'")


def test_privileged_requires_password_first():
    r = ScriptedRunner()
    with pytest.raises(AuthenticationRequired):
        r.run(["systemctl", "start", "a.service"], privileged=True)


def test_password_goes_to_stdin_not_command_line():
    r = ScriptedRunner()
    r.set_sudo_password("secret")
    r.run(["sh", "-c", "cat"], input="DATA", privileged=True)
    command, stdin = r.executed[-1]
    assert "secret" not in command
    assert command.startswith("sudo -k -S -p '' -- ")
    assert stdin == "secret\nDATA"


def test_wrong_password_is_rejected_before_use():
    r = ScriptedRunner()
    with pytest.raises(AuthenticationFailed):
        r.set_sudo_password("nope")
    assert r.needs_sudo_password


def test_nopasswd_and_root_modes():
    r = ScriptedRunner(nopasswd=True)
    r.run(["systemctl", "start", "a.service"], privileged=True)
    assert r.executed[-1] == ("sudo -n -- systemctl start a.service", None)

    r = ScriptedRunner(uid="0")
    r.run(["systemctl", "start", "a.service"], privileged=True)
    assert r.executed[-1] == ("systemctl start a.service", None)


def test_unknown_host_key_raises_and_trust_persists(tmp_path):
    pkey = paramiko.RSAKey.generate(1024)
    known = KnownHosts(tmp_path / "known_hosts", system_path=tmp_path / "missing")

    client = paramiko.SSHClient()
    known.apply(client)
    with pytest.raises(HostKeyUnknown) as info:
        client._policy.missing_host_key(client, "[srv]:2222", pkey)
    assert (info.value.hostname, info.value.port) == ("srv", 2222)
    assert info.value.fingerprint == fingerprint(pkey)

    known.trust("srv", 2222, pkey)
    client = paramiko.SSHClient()
    known.apply(client)
    assert client.get_host_keys().lookup("[srv]:2222")


def test_bad_host_key_is_translated(monkeypatch, tmp_path):
    good, bad = paramiko.RSAKey.generate(1024), paramiko.RSAKey.generate(1024)

    class Client(paramiko.SSHClient):
        def connect(self, **kwargs):
            raise paramiko.BadHostKeyException("h.example", bad, good)

    r = SSHRunner(Host("h", "h.example", "me"), KnownHosts(tmp_path / "kh", tmp_path / "x"), client_factory=Client)
    with pytest.raises(HostKeyMismatch):
        r.connect()


class RecordingClient(paramiko.SSHClient):
    calls: list = []

    def connect(self, **kwargs):
        RecordingClient.calls.append(kwargs)


def _runner(tmp_path, host, secret=None):
    return SSHRunner(host, KnownHosts(tmp_path / "kh", tmp_path / "none"), secret, client_factory=RecordingClient)


def test_key_auth_uses_only_the_chosen_key(tmp_path):
    RecordingClient.calls = []
    host = Host("h", "h.example", "me", auth=AuthMethod.KEY, key_path="/k")
    _runner(tmp_path, host, "passphrase").connect()
    kwargs = RecordingClient.calls[-1]
    assert kwargs["key_filename"] == "/k" and kwargs["passphrase"] == "passphrase"
    assert kwargs["allow_agent"] is False and kwargs["look_for_keys"] is False


def test_secret_is_dropped_after_connect(tmp_path):
    r = _runner(tmp_path, Host("h", "h.example", "me"), "pw")
    r.connect()
    assert r._secret is None


def test_cancel_before_connect(tmp_path):
    r = _runner(tmp_path, Host("h", "h.example", "me"), "pw")
    r.cancel()
    with pytest.raises(ConnectionCancelled):
        r.connect()


def test_cancel_during_connect_closes_the_socket(tmp_path):
    started, closed = threading.Event(), threading.Event()

    class BlockingClient(paramiko.SSHClient):
        def connect(self, **kwargs):
            started.set()
            if not closed.wait(5):
                raise AssertionError("connect was not interrupted")
            raise OSError("socket closed")

        def close(self):
            closed.set()

    r = SSHRunner(
        Host("h", "h.example", "me"), KnownHosts(tmp_path / "kh", tmp_path / "x"), "pw", client_factory=BlockingClient
    )
    errors = []
    t = threading.Thread(target=lambda: _capture(r.connect, errors))
    t.start()
    assert started.wait(5)
    r.cancel()
    t.join(5)
    assert not t.is_alive()
    assert isinstance(errors[0], ConnectionCancelled)
    assert not r.connected


def _capture(func, errors):
    try:
        func()
    except Exception as e:  # noqa: BLE001
        errors.append(e)
