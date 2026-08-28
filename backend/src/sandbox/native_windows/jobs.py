"""Windows Kernel Job Object resource containment."""

from __future__ import annotations

import ctypes
from typing import Any

from ..errors import SandboxInitializationError
from ..policy import ResourceLimits
from .api import _modules


class WindowsJobObject:
    """Kernel Job Object with kill-on-close, CPU, memory and process limits."""

    def __init__(self, name: str, limits: ResourceLimits) -> None:
        modules = _modules()
        self._api = modules["api"]
        self._job = modules["job"]
        try:
            self.handle = self._job.CreateJobObject(None, name)
            info = self._job.QueryInformationJobObject(
                self.handle,
                self._job.JobObjectExtendedLimitInformation,
            )
            basic = dict(info.get("BasicLimitInformation") or {})
            flags = (
                self._job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
                | self._job.JOB_OBJECT_LIMIT_ACTIVE_PROCESS
                | self._job.JOB_OBJECT_LIMIT_JOB_TIME
                | self._job.JOB_OBJECT_LIMIT_JOB_MEMORY
            )
            basic.update(
                {
                    "LimitFlags": flags,
                    "ActiveProcessLimit": limits.processes,
                    "PerJobUserTimeLimit": limits.cpu_seconds * 10_000_000,
                }
            )
            info["BasicLimitInformation"] = basic
            info["JobMemoryLimit"] = limits.memory_mib * 1024 * 1024
            self._job.SetInformationJobObject(
                self.handle,
                self._job.JobObjectExtendedLimitInformation,
                info,
            )
        except Exception as exc:  # pragma: no cover - Windows kernel adapter
            raise SandboxInitializationError("sandbox Job Object could not be configured") from exc

    def assign(self, process_handle: Any) -> None:
        try:
            self._job.AssignProcessToJobObject(self.handle, process_handle)
        except Exception as exc:  # pragma: no cover - Windows kernel adapter
            raise SandboxInitializationError("sandbox process could not enter its Job Object") from exc

    def terminate(self, exit_code: int = 1) -> None:
        try:
            self._job.TerminateJobObject(self.handle, exit_code)
        except Exception:
            pass

    def usage(self) -> dict[str, int | float]:
        """Return cumulative Job Object accounting without exposing PIDs."""

        try:
            accounting = self._job.QueryInformationJobObject(
                self.handle,
                self._job.JobObjectBasicAndIoAccountingInformation,
            )
            extended = self._job.QueryInformationJobObject(
                self.handle,
                self._job.JobObjectExtendedLimitInformation,
            )
            process_ids = self._job.QueryInformationJobObject(
                self.handle,
                self._job.JobObjectBasicProcessIdList,
            )
            basic = accounting.get("BasicInfo") or accounting.get("BasicAccountingInformation") or {}
            io = accounting.get("IoInfo") or accounting.get("IoAccountingInformation") or {}
            pids = tuple(int(value) for value in process_ids if int(value) > 0)
            handles = sum(self._handle_count(pid) for pid in pids)
            total_time_100ns = int(basic.get("TotalUserTime", 0)) + int(basic.get("TotalKernelTime", 0))
            return {
                "cpu_seconds": total_time_100ns / 10_000_000,
                "memory_bytes": int(extended.get("PeakJobMemoryUsed", 0)),
                "processes": len(pids),
                "handles": handles,
                "disk_bytes": int(io.get("WriteTransferCount", 0)),
            }
        except Exception as exc:  # pragma: no cover - Windows kernel adapter
            raise OSError("sandbox Job Object usage could not be sampled") from exc

    @staticmethod
    def _handle_count(pid: int) -> int:
        process = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if not process:
            return 0
        try:
            count = ctypes.c_uint32()
            if not ctypes.windll.kernel32.GetProcessHandleCount(process, ctypes.byref(count)):
                return 0
            return int(count.value)
        finally:
            ctypes.windll.kernel32.CloseHandle(process)

    def close(self) -> None:
        try:
            self._api.CloseHandle(self.handle)
        except Exception:
            pass
