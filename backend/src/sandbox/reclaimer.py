"""Background retry loop for Broker-owned sandbox resources."""

from __future__ import annotations

import logging
from collections.abc import Callable
from threading import Event, Thread

logger = logging.getLogger(__name__)


class SandboxResourceReclaimer:
    """Retry conservative Broker cleanup without changing Job terminal state."""

    def __init__(self, recover: Callable[[], tuple[str, ...]], *, interval_seconds: float = 5.0) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        self._recover = recover
        self._interval_seconds = interval_seconds
        self._stop = Event()
        self._wake = Event()
        self._thread: Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = Thread(target=self._run, name="sandbox-resource-reclaimer", daemon=True)
        self._thread.start()

    def notify(self) -> None:
        self._wake.set()

    def close(self, timeout: float = 10.0) -> None:
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)

    def recover_once(self) -> tuple[str, ...]:
        return tuple(self._recover())

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.recover_once()
            except Exception:
                logger.warning("sandbox resource recovery attempt failed", exc_info=False)
            self._wake.wait(self._interval_seconds)
            self._wake.clear()


__all__ = ["SandboxResourceReclaimer"]
