"""Persistent list of remote hosts."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from .errors import PilotError
from .models import AuthMethod, Host
from .paths import xdg_config_home
from .secrets import SecretStore

log = logging.getLogger(__name__)

LEGACY_CONFIG = xdg_config_home() / "systemd-manager" / "hosts.json"


class HostStore:
    """Hosts saved in ``hosts.json``; their secrets live in a :class:`SecretStore`."""

    def __init__(self, config_dir: Path, secrets: SecretStore, legacy_path: Path | None = LEGACY_CONFIG):
        self.path = config_dir / "hosts.json"
        self.secrets = secrets
        self._legacy_path = legacy_path
        self._hosts: dict[str, Host] = {}

    def load(self) -> list[Host]:
        self._hosts = {}
        if self.path.is_file():
            try:
                data = json.loads(self.path.read_text())
                for item in data.get("hosts", []):
                    host = Host.from_dict(item)
                    self._hosts[host.id] = host
            except (OSError, ValueError, KeyError, TypeError) as e:
                log.warning("Could not read %s: %s", self.path, e)
        elif self._legacy_path and self._legacy_path.is_file():
            self._import_legacy(self._legacy_path)
        return self.hosts()

    def hosts(self) -> list[Host]:
        return sorted(self._hosts.values(), key=lambda h: h.name.lower())

    def get(self, host_id: str) -> Host | None:
        return self._hosts.get(host_id)

    def save(self, host: Host, secret: str | None = None) -> None:
        """Add or update ``host``. ``secret=None`` keeps the stored secret."""
        host.name = host.name.strip()
        host.hostname = host.hostname.strip()
        host.username = host.username.strip()
        if not host.name or not host.hostname or not host.username:
            raise PilotError("Name, address and user name are required")
        if any(h.name.lower() == host.name.lower() and h.id != host.id for h in self._hosts.values()):
            raise PilotError(f"A host called “{host.name}” already exists")
        if host.auth is AuthMethod.KEY and not host.key_path:
            raise PilotError("Choose a private key file")
        if host.auth is AuthMethod.AGENT:
            secret = ""

        self._hosts[host.id] = host
        self._write()
        if secret == "":
            self.secrets.delete(host.id)
        elif secret is not None:
            self.secrets.set(host.id, f"systemd Pilot: {host.username}@{host.hostname}", secret)

    def remove(self, host_id: str) -> None:
        if self._hosts.pop(host_id, None) is not None:
            self._write()
        self.secrets.delete(host_id)

    def secret(self, host: Host) -> str | None:
        return self.secrets.get(host.id)

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {"version": 1, "hosts": [h.to_dict() for h in self.hosts()]}
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, self.path)

    def _import_legacy(self, path: Path) -> None:
        """Import hosts saved by systemd Pilot 3.x."""
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError) as e:
            log.warning("Could not read %s: %s", path, e)
            return
        for item in data.values():
            try:
                auth = AuthMethod.KEY if item.get("auth_type") == "key" else AuthMethod.PASSWORD
                host = Host(
                    name=item["name"],
                    hostname=item["hostname"],
                    username=item["username"],
                    auth=auth,
                    key_path=item.get("key_path"),
                )
            except (KeyError, TypeError, AttributeError):
                continue
            self._hosts[host.id] = host
            if auth is AuthMethod.PASSWORD:
                legacy = self.secrets.get_legacy(host.username, host.hostname)
                if legacy:
                    self.secrets.set(host.id, f"systemd Pilot: {host.username}@{host.hostname}", legacy)
        log.info("Imported %d host(s) from %s", len(self._hosts), path)
        self._write()
