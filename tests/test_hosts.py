import json
import stat

import pytest

from systemdpilot.core.errors import PilotError
from systemdpilot.core.hosts import HostStore
from systemdpilot.core.models import AuthMethod, Host
from systemdpilot.core.secrets import MemorySecretStore


class LegacySecrets(MemorySecretStore):
    def get_legacy(self, username, hostname):
        return "old-pw" if (username, hostname) == ("root", "srv") else None


def make_store(tmp_path, secrets=None, legacy=None):
    return HostStore(tmp_path / "cfg", secrets or MemorySecretStore(), legacy_path=legacy)


def test_roundtrip_and_permissions(tmp_path):
    store = make_store(tmp_path)
    host = Host("Web", "web.example", "admin", port=2222)
    store.save(host, "pw")

    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    reloaded = make_store(tmp_path, store.secrets)
    [loaded] = reloaded.load()
    assert loaded == host
    assert reloaded.secret(loaded) == "pw"


def test_edit_keeps_secret_and_id(tmp_path):
    store = make_store(tmp_path)
    host = Host("Web", "web.example", "admin")
    store.save(host, "pw")
    host.hostname = "web2.example"
    store.save(host)  # no new password entered
    assert store.secret(host) == "pw"
    assert len(store.hosts()) == 1


def test_remove_deletes_secret(tmp_path):
    store = make_store(tmp_path)
    host = Host("Web", "web.example", "admin")
    store.save(host, "pw")
    store.remove(host.id)
    assert store.hosts() == [] and store.secret(host) is None


def test_validation(tmp_path):
    store = make_store(tmp_path)
    store.save(Host("Web", "a", "u"))
    with pytest.raises(PilotError, match="already exists"):
        store.save(Host("web", "b", "u"))
    with pytest.raises(PilotError, match="required"):
        store.save(Host("", "b", "u"))
    with pytest.raises(PilotError, match="key"):
        store.save(Host("K", "b", "u", auth=AuthMethod.KEY))


def test_legacy_import(tmp_path):
    legacy = tmp_path / "old.json"
    legacy.write_text(
        json.dumps(
            {
                "srv": {
                    "name": "srv",
                    "hostname": "srv",
                    "username": "root",
                    "auth_type": "password",
                    "key_path": None,
                },
                "k": {"name": "k", "hostname": "k.example", "username": "me", "auth_type": "key", "key_path": "/id"},
                "broken": {"name": "x"},
            }
        )
    )
    store = make_store(tmp_path, LegacySecrets(), legacy)
    hosts = {h.name: h for h in store.load()}
    assert set(hosts) == {"srv", "k"}
    assert store.secret(hosts["srv"]) == "old-pw"
    assert hosts["k"].auth is AuthMethod.KEY and hosts["k"].key_path == "/id"
    assert store.path.is_file()
