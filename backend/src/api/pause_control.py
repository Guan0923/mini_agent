"""Thread-safe pause signalling for one active Turn."""

from __future__ import annotations

from collections.abc import Callable
from threading import Event, RLock


class TurnPauseController:
    """Linearize a user pause and fan it out to active operation aborters."""

    def __init__(self) -> None:
        self._requested = Event()
        self._lock = RLock()
        self._aborters: set[Callable[[], None]] = set()

    def is_requested(self) -> bool:
        return self._requested.is_set()

    def request_pause(self) -> bool:
        with self._lock:
            if self._requested.is_set():
                return False
            self._requested.set()
            aborters = tuple(self._aborters)
        for abort in aborters:
            try:
                abort()
            except Exception:
                continue
        return True

    def register_abort(self, abort: Callable[[], None]) -> Callable[[], None]:
        with self._lock:
            if self._requested.is_set():
                abort_now = True
            else:
                self._aborters.add(abort)
                abort_now = False
        if abort_now:
            abort()

        def unregister() -> None:
            with self._lock:
                self._aborters.discard(abort)

        return unregister


__all__ = ["TurnPauseController"]
