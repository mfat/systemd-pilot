"""Storage for SSH passwords and key passphrases."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod

from .errors import PilotError

log = logging.getLogger(__name__)

SCHEMA_NAME = "io.github.mfat.systemdpilot.Host"
LEGACY_KEYRING_SERVICE = "systemd-manager"


class SecretStore(ABC):
    @abstractmethod
    def get(self, host_id: str) -> str | None: ...

    @abstractmethod
    def set(self, host_id: str, label: str, secret: str) -> None: ...

    @abstractmethod
    def delete(self, host_id: str) -> None: ...

    def get_legacy(self, username: str, hostname: str) -> str | None:
        """Password saved by systemd Pilot 3.x, if any."""
        return None


class MemorySecretStore(SecretStore):
    def __init__(self):
        self._secrets: dict[str, str] = {}

    def get(self, host_id):
        return self._secrets.get(host_id)

    def set(self, host_id, label, secret):
        self._secrets[host_id] = secret

    def delete(self, host_id):
        self._secrets.pop(host_id, None)


class LibsecretStore(SecretStore):
    """Secrets in the desktop keyring (or the Secret portal inside Flatpak)."""

    def __init__(self):
        import gi

        gi.require_version("Secret", "1")
        from gi.repository import GLib, Secret

        self._Secret = Secret
        self._GLibError = GLib.Error
        self._schema = Secret.Schema.new(
            SCHEMA_NAME, Secret.SchemaFlags.NONE, {"host-id": Secret.SchemaAttributeType.STRING}
        )
        # Matches the attributes python-keyring used for 3.x passwords.
        self._legacy_schema = Secret.Schema.new(
            "org.freedesktop.Secret.Generic",
            Secret.SchemaFlags.DONT_MATCH_NAME,
            {"service": Secret.SchemaAttributeType.STRING, "username": Secret.SchemaAttributeType.STRING},
        )

    # Without a keyring service (no gnome-keyring, a headless session) every
    # call fails; behave as an empty store rather than breaking host management.

    def get(self, host_id):
        try:
            return self._Secret.password_lookup_sync(self._schema, {"host-id": host_id}, None)
        except self._GLibError as e:
            log.warning("Could not read from the keyring: %s", e.message)
            return None

    def set(self, host_id, label, secret):
        try:
            self._Secret.password_store_sync(
                self._schema, {"host-id": host_id}, self._Secret.COLLECTION_DEFAULT, label, secret, None
            )
        except self._GLibError as e:
            raise PilotError(f"Could not save the password in the keyring: {e.message}") from e

    def delete(self, host_id):
        try:
            self._Secret.password_clear_sync(self._schema, {"host-id": host_id}, None)
        except self._GLibError as e:
            log.warning("Could not remove a password from the keyring: %s", e.message)

    def get_legacy(self, username, hostname):
        try:
            return self._Secret.password_lookup_sync(
                self._legacy_schema, {"service": LEGACY_KEYRING_SERVICE, "username": f"{username}@{hostname}"}, None
            )
        except self._GLibError:
            return None
