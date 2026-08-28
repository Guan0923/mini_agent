"""Quota admission for sandbox jobs before a process is launched."""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass

from ..errors import SandboxError, SandboxFailureCode


@dataclass(frozen=True, slots=True)
class ResourceRequest:
    memory_mib: int = 0
    processes: int = 1
    handles: int = 0
    cpu_percent: float = 0.0


@dataclass(frozen=True, slots=True)
class AggregateLimits:
    memory_mib: int
    processes: int
    handles: int
    cpu_percent: float


def _empty_request() -> ResourceRequest:
    """Return a zero aggregate value without changing the one-job default."""

    return ResourceRequest(processes=0)


class SandboxAdmissionTimeout(SandboxError):
    def __init__(self) -> None:
        super().__init__("sandbox resource admission timed out", SandboxFailureCode.ADMISSION_TIMEOUT)


class SandboxAdmission:
    """Hierarchical user/system quota gate with a bounded FIFO wait."""

    def __init__(
        self,
        *,
        user_limits: AggregateLimits = AggregateLimits(8192, 512, 32768, 75.0),
        system_limits: AggregateLimits | None = None,
        wait_seconds: float = 30.0,
        available_memory_mib: int | None = None,
    ) -> None:
        self.user_limits = user_limits
        self.system_limits = system_limits or AggregateLimits(_system_memory_limit_mib(), 2048, 131072, 90.0)
        self.wait_seconds = min(max(wait_seconds, 0.0), 30.0)
        self.available_memory_mib = available_memory_mib
        self._condition = threading.Condition()
        self._user: dict[str, ResourceRequest] = {}
        self._system = _empty_request()

    def acquire(self, user_id: str, request: ResourceRequest, *, timeout: float | None = None) -> None:
        deadline = time.monotonic() + (self.wait_seconds if timeout is None else min(max(timeout, 0.0), 30.0))
        with self._condition:
            while not self._fits(user_id, request):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise SandboxAdmissionTimeout()
                self._condition.wait(remaining)
            self._user[user_id] = _add(self._user.get(user_id, _empty_request()), request)
            self._system = _add(self._system, request)

    def release(self, user_id: str, request: ResourceRequest) -> None:
        with self._condition:
            current = self._user.get(user_id, _empty_request())
            self._user[user_id] = _subtract(current, request)
            if self._user[user_id] == _empty_request():
                self._user.pop(user_id, None)
            self._system = _subtract(self._system, request)
            self._condition.notify_all()

    def usage(self, user_id: str | None = None) -> ResourceRequest:
        with self._condition:
            return self._user.get(user_id, _empty_request()) if user_id is not None else self._system

    def _fits(self, user_id: str, request: ResourceRequest) -> bool:
        user = _add(self._user.get(user_id, _empty_request()), request)
        system = _add(self._system, request)
        return _fits_limit(user, self.user_limits, self.available_memory_mib) and _fits_limit(
            system, self.system_limits, None
        )


def _add(left: ResourceRequest, right: ResourceRequest) -> ResourceRequest:
    return ResourceRequest(
        left.memory_mib + right.memory_mib,
        left.processes + right.processes,
        left.handles + right.handles,
        left.cpu_percent + right.cpu_percent,
    )


def _subtract(left: ResourceRequest, right: ResourceRequest) -> ResourceRequest:
    return ResourceRequest(
        max(0, left.memory_mib - right.memory_mib),
        max(0, left.processes - right.processes),
        max(0, left.handles - right.handles),
        max(0.0, left.cpu_percent - right.cpu_percent),
    )


def _fits_limit(value: ResourceRequest, limit: AggregateLimits, available_memory_mib: int | None) -> bool:
    memory_limit = limit.memory_mib
    if available_memory_mib is not None:
        memory_limit = min(memory_limit, max(0, int(available_memory_mib * 0.80)))
    return (
        value.memory_mib <= memory_limit
        and value.processes <= limit.processes
        and value.handles <= limit.handles
        and value.cpu_percent <= limit.cpu_percent
    )


def _system_memory_limit_mib() -> int:
    """Return the 80% machine-memory ceiling, with a conservative fallback."""

    total_bytes: int | None = None
    if os.name == "nt":
        try:
            import ctypes

            class _MemoryStatus(ctypes.Structure):
                _fields_ = [
                    ("length", ctypes.c_uint32),
                    ("memory_load", ctypes.c_uint32),
                    ("total_physical", ctypes.c_uint64),
                    ("available_physical", ctypes.c_uint64),
                    ("total_page_file", ctypes.c_uint64),
                    ("available_page_file", ctypes.c_uint64),
                    ("total_virtual", ctypes.c_uint64),
                    ("available_virtual", ctypes.c_uint64),
                    ("available_extended", ctypes.c_uint64),
                ]

            status = _MemoryStatus()
            status.length = ctypes.sizeof(_MemoryStatus)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                total_bytes = int(status.total_physical)
        except (AttributeError, OSError):
            total_bytes = None
    else:
        try:
            total_bytes = int(os.sysconf("SC_PHYS_PAGES")) * int(os.sysconf("SC_PAGE_SIZE"))
        except (AttributeError, OSError, ValueError):
            total_bytes = None
    if total_bytes is None or total_bytes <= 0:
        return 1_000_000
    return max(128, int(total_bytes * 0.80 / (1024 * 1024)))


__all__ = [
    "AggregateLimits",
    "ResourceRequest",
    "SandboxAdmission",
    "SandboxAdmissionTimeout",
]
