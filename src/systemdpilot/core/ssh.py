"""Run commands on a remote machine over SSH."""

from __future__ import annotations

import select
import shlex
import threading
import time
from collections.abc import Callable, Sequence

import paramiko

from .errors import (
    AuthenticationFailed,
    AuthenticationRequired,
    CommandError,
    ConnectionCancelled,
    ConnectionFailed,
    HostKeyUnknown,
)
from .known_hosts import KnownHosts, translate_bad_host_key
from .models import AuthMethod, Host
from .runner import CommandResult, CommandRunner

# Sessions without pam_systemd have no XDG_RUNTIME_DIR, which "systemctl --user" needs.
_ENV_PREFIX = 'SYSTEMD_COLORS=0 SYSTEMD_PAGER= XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}" '

# How sudo can be used on the remote host.
SUDO_ROOT = "root"  # we are already root
SUDO_NOPASSWD = "nopasswd"  # sudo works without a password
SUDO_PASSWORD = "password"  # sudo needs a password

_SUDO_FAILURE_HINTS = ("incorrect password", "sorry, try again", "a password is required", "authentication failure")


class SSHRunner(CommandRunner):
    """Run commands on ``host`` over a single SSH connection.

    Privileged commands use ``sudo``. The sudo password is sent on the
    command's standard input, never on its command line, so it does not
    show up in the remote process list.
    """

    def __init__(
        self,
        host: Host,
        known_hosts: KnownHosts,
        secret: str | None = None,
        *,
        client_factory: Callable[[], paramiko.SSHClient] = paramiko.SSHClient,
    ):
        self.host = host
        self.label = host.name
        self._known_hosts = known_hosts
        self._secret = secret
        self._client_factory = client_factory
        self._client: paramiko.SSHClient | None = None
        self._pending: paramiko.SSHClient | None = None  # client of an attempt in progress
        self._cancelled = False
        self._sudo_mode: str | None = None
        self._sudo_password: str | None = None
        self._lock = threading.Lock()  # guards _client, _pending and _cancelled
        self._probe_lock = threading.Lock()

    # -- connection -------------------------------------------------------

    @property
    def needs_stored_secret(self) -> bool:
        return self._secret is None and self.host.auth is not AuthMethod.AGENT

    def set_secret(self, secret: str | None) -> None:
        self._secret = secret

    @property
    def connected(self) -> bool:
        return self._active_transport() is not None

    def _active_transport(self) -> paramiko.Transport | None:
        # close() may run on another thread; read the client once, under the lock.
        with self._lock:
            client = self._client
        transport = client.get_transport() if client else None
        return transport if transport and transport.is_active() else None

    def connect(self, timeout: float = 15) -> None:
        """Connect; blocking. :meth:`cancel` from another thread aborts it."""
        client = self._client_factory()
        with self._lock:
            if self._cancelled:
                raise ConnectionCancelled("Connection cancelled")
            self._pending = client
        try:
            self._connect(client, timeout)
        finally:
            with self._lock:
                self._pending = None
                cancelled = self._cancelled
            # Only needed to unlock a key or log in; don't keep it around.
            self._secret = None
        if cancelled:
            client.close()
            raise ConnectionCancelled("Connection cancelled")
        with self._lock:
            self._client = client
        self._sudo_mode = None
        self._sudo_password = None

    def cancel(self) -> None:
        """Abort a :meth:`connect` running in another thread."""
        with self._lock:
            self._cancelled = True
            pending = self._pending
        if pending:
            # Closing the socket makes the blocked connect() fail promptly.
            pending.close()

    def _connect(self, client: paramiko.SSHClient, timeout: float) -> None:
        self._known_hosts.apply(client)
        h = self.host
        kwargs = dict(
            hostname=h.hostname,
            port=h.port,
            username=h.username,
            timeout=timeout,
            banner_timeout=timeout,
            auth_timeout=timeout,
        )
        if h.auth is AuthMethod.PASSWORD:
            kwargs.update(password=self._secret or "", allow_agent=False, look_for_keys=False)
        elif h.auth is AuthMethod.KEY:
            # Only the chosen key; agent keys would make the choice meaningless.
            kwargs.update(
                key_filename=h.key_path, passphrase=self._secret or None, allow_agent=False, look_for_keys=False
            )
        else:
            kwargs.update(allow_agent=True, look_for_keys=True)

        try:
            client.connect(**kwargs)
        except Exception as e:
            client.close()
            if self._cancelled:
                raise ConnectionCancelled("Connection cancelled") from None
            if isinstance(e, HostKeyUnknown):
                raise
            if isinstance(e, paramiko.BadHostKeyException):
                raise translate_bad_host_key(e) from e
            if isinstance(e, paramiko.AuthenticationException):
                raise AuthenticationFailed(f"Authentication to {h.hostname} failed: {e}") from e
            if isinstance(e, (TimeoutError, paramiko.SSHException, OSError, ValueError, EOFError)):
                raise ConnectionFailed(f"Could not connect to {h.hostname}: {e}") from e
            raise

    def close(self) -> None:
        with self._lock:
            client, self._client = self._client, None
        if client:
            client.close()
        self._sudo_password = None
        self._secret = None

    # -- sudo -------------------------------------------------------------

    def sudo_mode(self) -> str:
        with self._probe_lock:
            return self._probe_sudo_mode()

    def _probe_sudo_mode(self) -> str:
        if self._sudo_mode is None:
            uid = self._exec("id -u", None, 30)[1].strip()
            if uid == "0":
                self._sudo_mode = SUDO_ROOT
            elif self._exec("sudo -n true", None, 30)[0] == 0:
                self._sudo_mode = SUDO_NOPASSWD
            else:
                self._sudo_mode = SUDO_PASSWORD
        return self._sudo_mode

    @property
    def needs_sudo_password(self) -> bool:
        return self.sudo_mode() == SUDO_PASSWORD and self._sudo_password is None

    def set_sudo_password(self, password: str) -> None:
        """Check ``password`` with sudo and remember it for this connection."""
        # -k: ignore cached credentials so sudo always reads the password from stdin.
        code, _, err = self._exec("sudo -k -S -p '' true", password + "\n", 30)
        if code != 0:
            raise AuthenticationFailed(_sudo_error(err) or "Incorrect sudo password")
        self._sudo_password = password

    def forget_sudo_password(self) -> None:
        self._sudo_password = None

    # -- execution --------------------------------------------------------

    def run(
        self,
        argv: Sequence[str],
        *,
        input: str | None = None,
        privileged: bool = False,
        timeout: float | None = 60,
    ) -> CommandResult:
        command = shlex.join(argv)
        stdin = input
        if privileged:
            mode = self.sudo_mode()
            if mode == SUDO_NOPASSWD:
                command = "sudo -n -- " + command
            elif mode == SUDO_PASSWORD:
                if self._sudo_password is None:
                    raise AuthenticationRequired(f"sudo password required on {self.host.name}")
                command = "sudo -k -S -p '' -- " + command
                stdin = self._sudo_password + "\n" + (input or "")
        else:
            command = _ENV_PREFIX + command

        code, out, err = self._exec(command, stdin, timeout)
        if privileged and code != 0 and self._sudo_mode == SUDO_PASSWORD and _sudo_error(err):
            self._sudo_password = None
            raise AuthenticationFailed(_sudo_error(err))
        return CommandResult(tuple(argv), code, out, err)

    def _exec(self, command: str, stdin: str | None, timeout: float | None) -> tuple[int, str, str]:
        transport = self._active_transport()
        if transport is None:
            raise ConnectionFailed(f"Not connected to {self.host.name}")
        try:
            chan = transport.open_session(timeout=30)
            # The login shell may not be POSIX (fish, csh), so always hand the command to sh.
            chan.exec_command("sh -c " + shlex.quote(command))
            if stdin:
                chan.sendall(stdin.encode())
            chan.shutdown_write()
            return _collect(chan, timeout)
        except (paramiko.SSHException, OSError, EOFError) as e:
            raise ConnectionFailed(f"Lost connection to {self.host.name}: {e}") from e


def _sudo_error(stderr: str) -> str:
    lowered = stderr.lower()
    if any(hint in lowered for hint in _SUDO_FAILURE_HINTS):
        return "Incorrect sudo password"
    return ""


def _collect(chan: paramiko.Channel, timeout: float | None) -> tuple[int, str, str]:
    """Read stdout and stderr together so neither can fill up and stall."""
    out, err = bytearray(), bytearray()
    deadline = None if timeout is None else time.monotonic() + timeout
    try:
        while True:
            drained = True
            while chan.recv_ready():
                out += chan.recv(65536)
                drained = False
            while chan.recv_stderr_ready():
                err += chan.recv_stderr(65536)
                drained = False
            if drained and chan.exit_status_ready() and not chan.recv_ready() and not chan.recv_stderr_ready():
                break
            if deadline is not None and time.monotonic() > deadline:
                raise CommandError([], -1, "timed out")
            if drained:
                select.select([chan], [], [], 0.2)
        return chan.recv_exit_status(), out.decode("utf-8", "replace"), err.decode("utf-8", "replace")
    finally:
        chan.close()
