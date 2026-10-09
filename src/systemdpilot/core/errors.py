"""Exceptions raised by the business-logic layer."""

from __future__ import annotations

from collections.abc import Sequence


class PilotError(Exception):
    """Base class for all errors raised by systemd Pilot."""


class InvalidUnitName(PilotError):
    def __init__(self, name: str, reason: str):
        super().__init__(f"Invalid unit name “{name}”: {reason}")
        self.name = name
        self.reason = reason


class CommandError(PilotError):
    """A command ran but exited with a non-zero status."""

    def __init__(self, argv: Sequence[str], returncode: int, stderr: str):
        detail = stderr.strip() or f"exit status {returncode}"
        super().__init__(f"{' '.join(argv)}: {detail}")
        self.argv = list(argv)
        self.returncode = returncode
        self.stderr = stderr


class ParseError(PilotError):
    """Command output could not be understood."""


class AuthenticationRequired(PilotError):
    """A privileged command needs a password that has not been supplied yet."""


class AuthenticationFailed(PilotError):
    """The supplied password was rejected."""


class AuthenticationCancelled(PilotError):
    """The user dismissed the authentication prompt."""


class ConnectionFailed(PilotError):
    """An SSH connection could not be established or was lost."""


class HostKeyUnknown(PilotError):
    """The server presented a host key we have never seen.

    The caller may ask the user to verify ``fingerprint`` and then
    trust the key through :class:`~systemdpilot.core.known_hosts.KnownHosts`.
    """

    def __init__(self, hostname: str, port: int, key_type: str, fingerprint: str, key):
        super().__init__(f"The authenticity of host {hostname} can't be established")
        self.hostname = hostname
        self.port = port
        self.key_type = key_type
        self.fingerprint = fingerprint
        self.key = key


class HostKeyMismatch(PilotError):
    """The server's host key differs from the one we trusted before."""

    def __init__(self, hostname: str, fingerprint: str):
        super().__init__(
            f"The host key for {hostname} has changed (now {fingerprint}). "
            "This could mean someone is intercepting the connection."
        )
        self.hostname = hostname
        self.fingerprint = fingerprint


class UnitExists(PilotError):
    def __init__(self, path: str):
        super().__init__(f"{path} already exists")
        self.path = path
