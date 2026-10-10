"""Run systemd operations with authentication and error reporting."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from gi.repository import Gtk

from ..core.errors import AuthenticationCancelled, AuthenticationFailed, AuthenticationRequired, PilotError
from ..core.manager import SystemdManager
from ..core.ssh import SSHRunner
from ..i18n import _
from . import prompts
from .tasks import run_in_thread


class Operations:
    """Runs changes in the background and handles sudo prompts on remote hosts.

    Local privileged commands authenticate through polkit, so only SSH
    sessions ever ask for a password here.
    """

    def __init__(self, parent: Gtk.Widget, toast: Callable[[str], None]):
        self.parent = parent
        self.toast = toast

    def run(
        self,
        manager: SystemdManager,
        func: Callable[[], Any],
        *,
        on_success: Callable[[Any], None] | None = None,
        on_error: Callable[[BaseException], bool] | None = None,
        on_finish: Callable[[], None] | None = None,
        error_heading: str = "",
    ) -> None:
        """Run ``func`` in a thread.

        ``on_error`` may return True to say it handled the error itself.
        ``on_finish`` is called once, whatever the outcome.
        """

        def attempt():
            run_in_thread(func, on_done=done, on_error=failed)

        def done(result):
            if on_success:
                on_success(result)
            if on_finish:
                on_finish()

        def failed(error):
            runner = manager.runner
            if isinstance(error, AuthenticationRequired) and isinstance(runner, SSHRunner):
                self._ask_sudo(runner, attempt, cancelled)
                return
            if isinstance(error, AuthenticationFailed) and isinstance(runner, SSHRunner):
                self._ask_sudo(runner, attempt, cancelled, error=_("The password was incorrect. Try again."))
                return
            if isinstance(error, AuthenticationCancelled):
                cancelled()
                return
            if not (on_error and on_error(error)):
                prompts.show_error(self.parent, error_heading or _("Operation Failed"), describe(error))
            if on_finish:
                on_finish()

        def cancelled():
            self.toast(_("Authentication cancelled"))
            if on_finish:
                on_finish()

        attempt()

    def _ask_sudo(self, runner: SSHRunner, retry, cancelled, error: str = "") -> None:
        def on_password(password, _remember):
            if password is None:
                cancelled()
                return
            run_in_thread(
                runner.set_sudo_password,
                password,
                on_done=lambda _r: retry(),
                on_error=lambda e: self._ask_sudo(
                    runner,
                    retry,
                    cancelled,
                    error=_("The password was incorrect. Try again.")
                    if isinstance(e, AuthenticationFailed)
                    else describe(e),
                ),
            )

        prompts.ask_password(
            self.parent,
            _("Administrator Password Required"),
            _("Enter the sudo password for {user} on {host}.").format(user=runner.host.username, host=runner.host.name),
            on_password,
            error=error,
        )


def describe(error: BaseException) -> str:
    if isinstance(error, PilotError):
        return str(error)
    return f"{type(error).__name__}: {error}"
