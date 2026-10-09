"""Abstract command execution.

A :class:`CommandRunner` executes an argv list somewhere (this machine,
or a remote one over SSH), optionally with elevated privileges. The
:class:`~systemdpilot.core.manager.SystemdManager` builds argv lists and
never deals with shells, sudo, or quoting itself.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass

from .errors import CommandError


@dataclass(frozen=True)
class CommandResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def check(self) -> CommandResult:
        if not self.ok:
            raise CommandError(self.argv, self.returncode, self.stderr)
        return self


class CommandRunner(ABC):
    #: Human-readable name of the machine, for messages.
    label: str = ""

    @abstractmethod
    def run(
        self,
        argv: Sequence[str],
        *,
        input: str | None = None,
        privileged: bool = False,
        timeout: float | None = 60,
    ) -> CommandResult:
        """Run ``argv`` and return its result.

        Does not raise on a non-zero exit status; call
        :meth:`CommandResult.check` for that. May raise
        :class:`~systemdpilot.core.errors.AuthenticationRequired`,
        :class:`~systemdpilot.core.errors.AuthenticationFailed`,
        :class:`~systemdpilot.core.errors.AuthenticationCancelled` or
        :class:`~systemdpilot.core.errors.ConnectionFailed`.
        """

    def close(self) -> None:
        """Release any resources (connections)."""
