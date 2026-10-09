"""Run commands on this machine."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Sequence

from .errors import AuthenticationCancelled, AuthenticationFailed, CommandError
from .runner import CommandResult, CommandRunner

# pkexec exit codes, see pkexec(1)
_PKEXEC_DENIED = 126
_PKEXEC_AUTH_FAILED = 127

_ENV = {"SYSTEMD_COLORS": "0", "SYSTEMD_PAGER": "", "SYSTEMD_LESS": ""}


def in_flatpak() -> bool:
    return os.path.exists("/.flatpak-info")


class LocalRunner(CommandRunner):
    """Run commands locally, escaping the Flatpak sandbox when needed.

    Privileged commands go through ``pkexec`` so authentication is handled
    by the desktop's polkit agent; this app never sees the password.
    """

    label = "this computer"

    def __init__(self, *, sandboxed: bool | None = None):
        self.sandboxed = in_flatpak() if sandboxed is None else sandboxed

    def build_argv(self, argv: Sequence[str], privileged: bool) -> list[str]:
        cmd = list(argv)
        if privileged:
            cmd = ["pkexec", *cmd]
        if self.sandboxed:
            env = [f"--env={k}={v}" for k, v in _ENV.items()]
            cmd = ["flatpak-spawn", "--host", "--watch-bus", *env, *cmd]
        return cmd

    def run(
        self,
        argv: Sequence[str],
        *,
        input: str | None = None,
        privileged: bool = False,
        timeout: float | None = 60,
    ) -> CommandResult:
        cmd = self.build_argv(argv, privileged)
        env = {**os.environ, **_ENV}
        try:
            proc = subprocess.run(
                cmd,
                input=input,
                capture_output=True,
                text=True,
                errors="replace",
                env=env,
                # Polkit dialogs need time for a human to respond.
                timeout=None if privileged else timeout,
                stdin=None if input is not None else subprocess.DEVNULL,
            )
        except FileNotFoundError as e:
            raise CommandError(cmd, 127, f"{e.filename}: command not found") from e
        except subprocess.TimeoutExpired as e:
            raise CommandError(cmd, -1, "timed out") from e

        if privileged and proc.returncode == _PKEXEC_DENIED:
            raise AuthenticationCancelled("Authentication was cancelled")
        if privileged and proc.returncode == _PKEXEC_AUTH_FAILED and "Not authorized" in proc.stderr:
            raise AuthenticationFailed("Not authorized")
        return CommandResult(tuple(argv), proc.returncode, proc.stdout, proc.stderr)
