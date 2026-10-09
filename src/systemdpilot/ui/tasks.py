"""Run blocking work off the main thread and deliver results back to it."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any

from gi.repository import GLib

log = logging.getLogger(__name__)


def run_in_thread(
    func: Callable[..., Any],
    *args: Any,
    on_done: Callable[[Any], None] | None = None,
    on_error: Callable[[BaseException], None] | None = None,
) -> None:
    """Call ``func(*args)`` in a worker thread.

    ``on_done(result)`` or ``on_error(exception)`` is then called on the
    GTK main thread. GTK must only be touched from those callbacks.
    """

    def deliver(callback, value):
        callback(value)
        return GLib.SOURCE_REMOVE

    def worker():
        try:
            result = func(*args)
        except Exception as e:  # noqa: BLE001 - every failure is reported to the UI
            log.debug("Background task %s failed", getattr(func, "__name__", func), exc_info=True)
            if on_error:
                GLib.idle_add(deliver, on_error, e)
            else:
                log.exception("Unhandled error in background task")
            return
        if on_done:
            GLib.idle_add(deliver, on_done, result)

    threading.Thread(target=worker, daemon=True).start()
