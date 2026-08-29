"""Process-wide admission gate for destructive Sandbox maintenance."""

from __future__ import annotations

from threading import RLock


class SandboxMaintenanceBusy(RuntimeError):
    """Raised when maintenance and command execution would overlap."""


class SandboxCommandLease:
    def __init__(self, gate: SandboxMaintenanceGate) -> None:
        self._gate = gate
        self._closed = False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._gate._release_command()

    def __enter__(self) -> SandboxCommandLease:
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.close()


class SandboxMaintenanceLease:
    def __init__(self, gate: SandboxMaintenanceGate) -> None:
        self._gate = gate
        self._closed = False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._gate._release_maintenance()

    def __enter__(self) -> SandboxMaintenanceLease:
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.close()


class SandboxMaintenanceGate:
    """Non-blocking shared/exclusive gate for commands and reinstall."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._active_commands = 0
        self._maintenance = False

    @property
    def active_commands(self) -> int:
        with self._lock:
            return self._active_commands

    @property
    def maintenance_active(self) -> bool:
        with self._lock:
            return self._maintenance

    def acquire_command(self) -> SandboxCommandLease:
        with self._lock:
            if self._maintenance:
                raise SandboxMaintenanceBusy("Sandbox Broker maintenance is in progress")
            self._active_commands += 1
        return SandboxCommandLease(self)

    def acquire_maintenance(self) -> SandboxMaintenanceLease:
        with self._lock:
            if self._maintenance or self._active_commands:
                raise SandboxMaintenanceBusy("Sandbox commands are active")
            self._maintenance = True
        return SandboxMaintenanceLease(self)

    def _release_command(self) -> None:
        with self._lock:
            if self._active_commands <= 0:
                raise RuntimeError("Sandbox command lease underflow")
            self._active_commands -= 1

    def _release_maintenance(self) -> None:
        with self._lock:
            if not self._maintenance:
                raise RuntimeError("Sandbox maintenance lease is not active")
            self._maintenance = False


__all__ = [
    "SandboxCommandLease",
    "SandboxMaintenanceBusy",
    "SandboxMaintenanceGate",
    "SandboxMaintenanceLease",
]
