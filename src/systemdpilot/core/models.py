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

    @property
    def past_tense(self) -> str:
        return {
            UnitAction.START: "started",
            UnitAction.STOP: "stopped",
            UnitAction.RESTART: "restarted",
            UnitAction.RELOAD: "reloaded",
            UnitAction.ENABLE: "enabled",
            UnitAction.DISABLE: "disabled",
        }[self]


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


@dataclass(frozen=True)
class LogResult:
    entries: list[LogEntry]
    warning: str = ""  # e.g. journalctl permission hints
