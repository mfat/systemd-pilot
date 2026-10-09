"""SSH host key verification."""

from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path

import paramiko

from .errors import HostKeyMismatch, HostKeyUnknown


def fingerprint(key: paramiko.PKey) -> str:
    digest = hashlib.sha256(key.asbytes()).digest()
    return "SHA256:" + base64.b64encode(digest).decode().rstrip("=")


def host_entry(hostname: str, port: int) -> str:
    return hostname if port == 22 else f"[{hostname}]:{port}"


class KnownHosts:
    """Host keys trusted by the user.

    Keys from ``~/.ssh/known_hosts`` are honoured but never modified; keys the
    user accepts in this app are stored in a separate file.
    """

    def __init__(self, path: Path, system_path: Path | None = None):
        self.path = path
        self.system_path = system_path if system_path is not None else Path.home() / ".ssh" / "known_hosts"

    def apply(self, client: paramiko.SSHClient) -> None:
        if self.system_path.is_file():
            try:
                client.load_system_host_keys(str(self.system_path))
            except (OSError, paramiko.SSHException):
                pass
        if self.path.is_file():
            client.load_host_keys(str(self.path))
        client.set_missing_host_key_policy(_RaiseUnknown())

    def trust(self, hostname: str, port: int, key: paramiko.PKey) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        keys = paramiko.HostKeys()
        if self.path.is_file():
            keys.load(str(self.path))
        keys.add(host_entry(hostname, port), key.get_name(), key)
        tmp = self.path.with_suffix(".tmp")
        keys.save(str(tmp))
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)


class _RaiseUnknown(paramiko.MissingHostKeyPolicy):
    def missing_host_key(self, client, hostname, key):
        host, port = hostname, 22
        if hostname.startswith("[") and "]:" in hostname:
            host, _, port_s = hostname[1:].partition("]:")
            port = int(port_s)
        raise HostKeyUnknown(host, port, key.get_name(), fingerprint(key), key)


def translate_bad_host_key(error: paramiko.BadHostKeyException) -> HostKeyMismatch:
    return HostKeyMismatch(error.hostname, fingerprint(error.key))
