"""Best-effort resource accounting with injectable providers for Windows tests."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Protocol

from .errors import SandboxResourceExceeded
from .policy import SandboxLimits


@dataclass(frozen=True, slots=True)
class ResourceUsage:
    wall_seconds: float = 0.0
    cpu_seconds: float = 0.0
    memory_bytes: int = 0
    processes: int = 1
    handles: int = 0
    output_chars: int = 0
    disk_bytes: int = 0


class ResourceProvider(Protocol):
    def sample(self, pid: int) -> ResourceUsage: ...


class ResourceMonitor:
    """Poll process-tree usage and terminate on the first hard violation."""

    def __init__(
        self,
        pid: int,
        limits: SandboxLimits,
        *,
        provider: ResourceProvider,
        interval_seconds: float = 0.25,
        on_exceeded=None,
    ) -> None:
        limits.validate()
        self.pid = pid
        self.limits = limits
        self.provider = provider
        self.interval_seconds = max(0.05, interval_seconds)
        self.on_exceeded = on_exceeded
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_usage = ResourceUsage()

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name=f"sandbox-resource-{self.pid}", daemon=True)
        self._thread.start()

    def stop(self, timeout: float | None = 2.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)

    def check(self, usage: ResourceUsage) -> None:
        self.last_usage = usage
        checks = (
            (usage.wall_seconds > self.limits.wall_seconds, "wall time limit exceeded"),
            (usage.cpu_seconds > self.limits.cpu_seconds, "CPU limit exceeded"),
            (usage.memory_bytes > self.limits.memory_mib * 1024 * 1024, "memory limit exceeded"),
            (usage.processes > self.limits.processes, "process limit exceeded"),
            (usage.handles > self.limits.handles, "handle limit exceeded"),
            (usage.output_chars > self.limits.output_chars, "output limit exceeded"),
            (self.limits.disk_mib > 0 and usage.disk_bytes > self.limits.disk_mib * 1024 * 1024, "disk limit exceeded"),
        )
        for exceeded, message in checks:
            if exceeded:
                raise SandboxResourceExceeded(message)

    def _run(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            try:
                usage = self.provider.sample(self.pid)
                self.check(usage)
            except SandboxResourceExceeded as exc:
                if callable(self.on_exceeded):
                    self.on_exceeded(exc)
                return
            except ProcessLookupError:
                return
            except OSError:
                # Losing the accounting channel while the process may still
                # be alive is a safety failure. The caller terminates the
                # process tree instead of running without enforcement.
                if callable(self.on_exceeded):
                    self.on_exceeded(SandboxResourceExceeded("sandbox resource accounting failed"))
                return


class NullResourceProvider:
    """Provider used by callers that only need lifecycle/limit validation."""

    def sample(self, _pid: int) -> ResourceUsage:
        return ResourceUsage()


__all__ = ["NullResourceProvider", "ResourceMonitor", "ResourceProvider", "ResourceUsage"]
