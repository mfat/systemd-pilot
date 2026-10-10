"""Plain data types shared by the core and the UI."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class Scope(str, Enum):
    """Which systemd instance to talk to."""

    SYSTEM = "system"
    USER = "user"


class UnitAction(str, Enum):
    START = "start"
    STOP = "stop"
    RESTART = "restart"
    RELOAD = "reload"
    ENABLE = "enable"
    DISABLE = "disable"


class AuthMethod(str, Enum):
    PASSWORD = "password"
    KEY = "key"
    AGENT = "agent"


@dataclass(frozen=True)
class Unit:
    name: str
    description: str = ""
    load_state: str = ""
    active_state: str = "inactive"
    sub_state: str = ""
    # enabled, disabled, static, masked, …; "" if there is no unit file, None if not known yet
    file_state: str | None = None
    # Filled in with the startup states; see SystemdManager.complete_units.
    main_pid: int = 0
    memory: int | None = None  # bytes
    since: datetime | None = None  # when it entered its current state
    # Which systemd runs it: the machine's, or the user's own (systemctl --user).
    scope: Scope = Scope.SYSTEM

    @property
    def key(self) -> tuple[Scope, str]:
        """Identifies the unit: the same name can exist as a system and a user unit."""
        return self.scope, self.name

    @property
    def is_user(self) -> bool:
        return self.scope is Scope.USER

    @property
    def short_name(self) -> str:
        return self.name.removesuffix(".service")

    @property
    def is_active(self) -> bool:
        return self.active_state in ("active", "reloading", "activating")

    @property
    def is_failed(self) -> bool:
        return self.active_state == "failed"

    @property
    def kind(self) -> str:
        """``failed``, ``running``, ``exited`` (ran and finished) or ``dead`` (not running)."""
        if self.is_failed:
            return "failed"
        if self.active_state == "active" and self.sub_state == "exited":
            return "exited"
        if self.is_active or self.active_state == "deactivating":
            return "running"
        return "dead"

    @property
    def state_label(self) -> str:
        if self.sub_state and self.sub_state != self.active_state:
            return f"{self.active_state} ({self.sub_state})"
        return self.active_state


@dataclass
class Host:
    name: str
    hostname: str
    username: str
    port: int = 22
    auth: AuthMethod = AuthMethod.PASSWORD
    key_path: str | None = None
    id: str = field(default_factory=lambda: uuid.uuid4().hex)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "hostname": self.hostname,
            "port": self.port,
            "username": self.username,
            "auth": self.auth.value,
            "key_path": self.key_path,
        }

    @classmethod
    def from_dict(cls, data: dict) -> Host:
        return cls(
            id=data.get("id") or uuid.uuid4().hex,
            name=data["name"],
            hostname=data["hostname"],
            port=int(data.get("port", 22)),
            username=data["username"],
            auth=AuthMethod(data.get("auth", AuthMethod.PASSWORD.value)),
            key_path=data.get("key_path"),
        )


@dataclass(frozen=True)
class LogEntry:
    timestamp: datetime | None
    priority: int  # syslog priority, 0 (emerg) … 7 (debug)
    identifier: str
    pid: str
    message: str
    unit: str = ""  # the unit the entry is about, or comes from
    boot_id: str = ""
    kernel: bool = False


@dataclass(frozen=True)
class LogResult:
    entries: list[LogEntry]
    warning: str = ""  # e.g. journalctl permission hints
