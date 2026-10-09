import os
import stat

import paramiko
import pytest

from systemdpilot.core.errors import ConnectionFailed
from systemdpilot.core.known_hosts import KnownHosts


@pytest.fixture(scope="module")
def key():
    return paramiko.RSAKey.generate(1024)


def entry(host, key):
    return f"{host} {key.get_name()} {key.get_base64()}"


def loaded(known):
    client = paramiko.SSHClient()
    known.apply(client)
    return client.get_host_keys()


def test_bad_lines_do_not_discard_good_ones(tmp_path, key):
    system = tmp_path / "known_hosts"
    system.write_text(
        "# comment\n"
        "broken ssh-rsa !!!notbase64!!!\n"
        "onlyonefield\n"
        "@cert-authority *.example.com " + f"{key.get_name()} {key.get_base64()}\n" + entry("good.example", key) + "\n"
    )
    keys = loaded(KnownHosts(tmp_path / "app", system_path=system))
    assert keys.lookup("good.example") is not None
    assert keys.lookup("broken") is None


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read any file")
def test_unreadable_file_is_an_error(tmp_path, key):
    system = tmp_path / "known_hosts"
    system.write_text(entry("good.example", key) + "\n")
    system.chmod(0)
    try:
        with pytest.raises(ConnectionFailed, match="Could not read"):
            loaded(KnownHosts(tmp_path / "app", system_path=system))
    finally:
        system.chmod(0o600)


def test_trusted_keys_file_is_private(tmp_path, key):
    known = KnownHosts(tmp_path / "app" / "known_hosts", system_path=tmp_path / "none")
    known.trust("srv", 22, key)
    assert stat.S_IMODE(known.path.stat().st_mode) == 0o600
    assert loaded(known).lookup("srv") is not None
