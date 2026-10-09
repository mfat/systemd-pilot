"""SSH host key verification."""

from __future__ import annotations

import base64
import hashlib
import logging
import os
from pathlib import Path

import paramiko

from .errors import ConnectionFailed, HostKeyMismatch, HostKeyUnknown

log = logging.getLogger(__name__)


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
        """Load trusted keys into ``client``.

        Like OpenSSH, malformed lines are skipped and the rest still count.
        (paramiko would drop the whole file on the first bad line, turning
        pinned hosts into "unknown" ones the user might accept.) A file that
        exists but can't be read at all is an error, for the same reason.
        """
        _load_lenient(self.system_path, client.get_host_keys())
        _load_lenient(self.path, client.get_host_keys())
        client.set_missing_host_key_policy(_RaiseUnknown())

    def trust(self, hostname: str, port: int, key: paramiko.PKey) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        keys = paramiko.HostKeys()
        if self.path.is_file():
            keys.load(str(self.path))
        keys.add(host_entry(hostname, port), key.get_name(), key)
        tmp = self.path.with_suffix(".tmp")
        # Create the file private before writing; HostKeys.save keeps its mode.
        os.close(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600))
        os.chmod(tmp, 0o600)
        keys.save(str(tmp))
        os.replace(tmp, self.path)


def _load_lenient(path: Path, keys: paramiko.HostKeys) -> int:
    """Add the valid entries of known_hosts file ``path`` to ``keys``; return how many were skipped."""
    if not path.is_file():
        return 0
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError as e:
        raise ConnectionFailed(f"Could not read {path}: {e}") from e
    skipped = 0
    for lineno, line in enumerate(lines, 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            entry = paramiko.hostkeys.HostKeyEntry.from_line(line, lineno)
        except (paramiko.SSHException, paramiko.hostkeys.InvalidHostKey, ValueError):
            entry = None
        if entry is None:  # malformed, or a marker/key type paramiko doesn't support
            skipped += 1
            continue
        for hostname in entry.hostnames:
            keys.add(hostname, entry.key.get_name(), entry.key)
    if skipped:
        log.warning("Skipped %d unusable line(s) in %s", skipped, path)
    return skipped


class _RaiseUnknown(paramiko.MissingHostKeyPolicy):
    def missing_host_key(self, client, hostname, key):
        host, port = hostname, 22
        if hostname.startswith("[") and "]:" in hostname:
            host, _, port_s = hostname[1:].partition("]:")
            port = int(port_s)
        raise HostKeyUnknown(host, port, key.get_name(), fingerprint(key), key)


def translate_bad_host_key(error: paramiko.BadHostKeyException) -> HostKeyMismatch:
    return HostKeyMismatch(error.hostname, fingerprint(error.key))
