"""Open connections to machines."""

from __future__ import annotations

import threading

from .hosts import HostStore
from .known_hosts import KnownHosts
from .local import LocalRunner
from .manager import SystemdManager
from .models import Host
from .ssh import SSHRunner

LOCAL_ID = "local"


class Sessions:
    """Keeps one :class:`SystemdManager` per connected machine.

    The local machine is always available under :data:`LOCAL_ID`.
    """

    def __init__(self, hosts: HostStore, known_hosts: KnownHosts, local_runner: LocalRunner | None = None):
        self.hosts = hosts
        self.known_hosts = known_hosts
        self._managers: dict[str, SystemdManager] = {LOCAL_ID: SystemdManager(local_runner or LocalRunner())}
        self._lock = threading.Lock()

    def get(self, machine_id: str) -> SystemdManager | None:
        with self._lock:
            return self._managers.get(machine_id)

    def is_connected(self, machine_id: str) -> bool:
        return self.get(machine_id) is not None

    def connect(self, host: Host, secret: str | None = None) -> SystemdManager:
        """Connect to ``host``. Blocking; call from a worker thread.

        ``secret`` overrides the stored password or key passphrase.
        """
        runner = SSHRunner(host, self.known_hosts, secret if secret is not None else self.hosts.secret(host))
        runner.connect()
        manager = SystemdManager(runner)
        with self._lock:
            old = self._managers.pop(host.id, None)
            self._managers[host.id] = manager
        if old:
            old.runner.close()
        return manager

    def disconnect(self, machine_id: str) -> None:
        if machine_id == LOCAL_ID:
            return
        with self._lock:
            manager = self._managers.pop(machine_id, None)
        if manager:
            manager.runner.close()

    def close_all(self) -> None:
        for machine_id in list(self._managers):
            self.disconnect(machine_id)
