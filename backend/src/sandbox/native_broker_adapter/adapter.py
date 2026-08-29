"""Native Broker adapter for fixed credentials, tokens, processes and Jobs."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import threading
import time
import uuid
from base64 import b64decode, b64encode
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..broker_service.credentials import BrokerCredentialPackage
from ..errors import SandboxInitializationError, SandboxResourceExceeded
from ..native_windows import (
    WindowsJobObject,
    WindowsPrivateDesktop,
    WindowsRestrictedTokenFactory,
    WindowsSandboxAccount,
)
from ..native_windows.api import _modules
from ..policy import FileAccessMode, NetworkMode, ResourceLimits
from ..runtime.resources import ResourceMonitor, ResourceUsage
from .process import _NativeWindowsProcess


@dataclass(slots=True)
class _Reservation:
    reservation_id: str
    backend_instance_id: str
    user_id: str
    job_id: str
    policy_hash: str
    policy: dict[str, Any]
    token: Any
    logon_sid: str
    account_sid: str
    workspace_cap_sid: str
    temp_cap_sid: str
    capability_digest: str
    desktop: WindowsPrivateDesktop
    expires_at: float


@dataclass(slots=True)
class _NativeLease:
    process_id: str
    backend_instance_id: str
    user_id: str
    job_id: str
    process: _NativeWindowsProcess
    desktop: WindowsPrivateDesktop
    resource_monitor: ResourceMonitor | None = None
    failure_code: str | None = None


class _NativeResourceProvider:
    def __init__(self, process: _NativeWindowsProcess) -> None:
        self.process = process
        self.started_at = time.monotonic()

    def sample(self, _pid: int) -> ResourceUsage:
        usage = self.process.job.usage()
        return ResourceUsage(
            wall_seconds=time.monotonic() - self.started_at,
            cpu_seconds=float(usage["cpu_seconds"]),
            memory_bytes=int(usage["memory_bytes"]),
            processes=int(usage["processes"]),
            handles=int(usage["handles"]),
            output_chars=self.process.output_bytes,
            disk_bytes=int(usage["disk_bytes"]),
        )


class WindowsNativeBrokerAdapter:
    """Own only reservation tokens, child processes, Job Objects and cleanup."""

    def __init__(
        self,
        *,
        credentials: BrokerCredentialPackage,
        service_sid: str,
        token_factory: WindowsRestrictedTokenFactory | None = None,
        desktop_factory=None,
        clock=None,
        reservation_ttl_seconds: int = 30,
    ) -> None:
        if not 1 <= reservation_ttl_seconds <= 60:
            raise ValueError("reservation TTL must be between 1 and 60 seconds")
        self.credentials = credentials
        self.service_sid = service_sid
        self.token_factory = token_factory or WindowsRestrictedTokenFactory(service_sid)
        self.desktop_factory = desktop_factory or WindowsPrivateDesktop.create
        self._clock = clock or time.monotonic
        self._reservation_ttl_seconds = reservation_ttl_seconds
        self._reservations: dict[str, _Reservation] = {}
        self._processes: dict[str, _NativeLease] = {}
        self._jobs: dict[tuple[str, str, str], str] = {}
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._expiry_thread = threading.Thread(target=self._expire_loop, name="sandbox-reservations", daemon=True)
        self._expiry_thread.start()

    def close(self) -> None:
        self._stop.set()
        self._expiry_thread.join(timeout=2.0)
        with self._lock:
            reservations = tuple(self._reservations.values())
            leases = tuple(self._processes.values())
            self._reservations.clear()
            self._processes.clear()
            self._jobs.clear()
        for reservation in reservations:
            self._close_reservation(reservation)
        for lease in leases:
            lease.process.close()
            lease.desktop.close()

    def reserve(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        policy = request.get("policy")
        if not isinstance(policy, Mapping):
            raise SandboxInitializationError("Broker reserve policy is invalid")
        policy_value = dict(policy)
        policy_hash = _required_text(request, "policy_hash")
        if not hmac.compare_digest(policy_hash, _policy_hash(policy_value)):
            raise SandboxInitializationError("Broker reserve policy hash is invalid")
        backend_id = _required_text(request, "backend_instance_id")
        user_id = _required_text(request, "user_id")
        job_id = _required_text(policy_value, "job_id")
        file_mode = FileAccessMode(str(policy_value.get("file_mode") or FileAccessMode.READ_ONLY.value))
        network_mode = NetworkMode(str(policy_value.get("network_mode") or NetworkMode.NO_NETWORK.value))
        account = self._account(network_mode)
        reserved = self.token_factory.reserve(account, file_mode)
        reservation_id = f"reservation-{uuid.uuid4().hex}"
        workspace = _canonical_absolute_path(_required_text(policy_value, "workspace"))
        temp_dir = _canonical_absolute_path(_required_text(policy_value, "temp_dir"))
        cwd = _canonical_absolute_path(_required_text(policy_value, "cwd"))
        if not cwd.is_relative_to(workspace):
            _close_handle(reserved.token)
            raise SandboxInitializationError("Broker reserve cwd is outside the workspace")
        expires_at = self._clock() + self._reservation_ttl_seconds
        capability_digest = _capability_digest(
            reservation_id=reservation_id,
            policy_hash=policy_hash,
            workspace=workspace,
            temp_dir=temp_dir,
            account_sid=reserved.account_sid,
            workspace_cap_sid=reserved.workspace_cap_sid,
            temp_cap_sid=reserved.temp_cap_sid,
            expires_at=expires_at,
        )
        try:
            desktop = self.desktop_factory(reserved.logon_sid, self.service_sid)
        except Exception:
            _close_handle(reserved.token)
            raise
        reservation = _Reservation(
            reservation_id,
            backend_id,
            user_id,
            job_id,
            policy_hash,
            policy_value,
            reserved.token,
            reserved.logon_sid,
            reserved.account_sid,
            reserved.workspace_cap_sid,
            reserved.temp_cap_sid,
            capability_digest,
            desktop,
            expires_at,
        )
        with self._lock:
            self._purge_expired_locked()
            identity = (backend_id, user_id, job_id)
            if identity in self._jobs or any(
                (item.backend_instance_id, item.user_id, item.job_id) == identity
                for item in self._reservations.values()
            ):
                self._close_reservation(reservation)
                raise SandboxInitializationError("Broker job already exists")
            self._reservations[reservation_id] = reservation
        return {
            "reserved": True,
            "reservation_id": reservation_id,
            "logon_sid": reserved.logon_sid,
            "account_sid": reserved.account_sid,
            "service_sid": self.service_sid,
            "capability_sids": {
                "workspace": reserved.workspace_cap_sid,
                "temp": reserved.temp_cap_sid,
            },
            "capability_digest": capability_digest,
            "expires_in": self._reservation_ttl_seconds,
        }

    def launch(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        reservation_id = _required_text(request, "reservation_id")
        backend_id = _required_text(request, "backend_instance_id")
        user_id = _required_text(request, "user_id")
        policy_hash = _required_text(request, "policy_hash")
        capability_digest = _required_text(request, "capability_digest")
        with self._lock:
            self._purge_expired_locked()
            reservation = self._reservations.pop(reservation_id, None)
        if reservation is None:
            raise SandboxInitializationError("Broker reservation is unavailable")
        if (
            reservation.backend_instance_id != backend_id
            or reservation.user_id != user_id
            or not hmac.compare_digest(reservation.policy_hash, policy_hash)
            or not hmac.compare_digest(reservation.capability_digest, capability_digest)
        ):
            self._close_reservation(reservation)
            raise SandboxInitializationError("Broker reservation ownership is invalid")
        argv = request.get("argv")
        environment = request.get("environment")
        if not isinstance(argv, list) or not argv or not all(isinstance(item, str) and item for item in argv):
            self._close_reservation(reservation)
            raise SandboxInitializationError("Broker launch argv is invalid")
        if not isinstance(environment, Mapping):
            self._close_reservation(reservation)
            raise SandboxInitializationError("Broker launch environment is invalid")
        policy = reservation.policy
        workspace = _canonical_absolute_path(_required_text(policy, "workspace"))
        cwd = _canonical_absolute_path(_required_text(request, "cwd"))
        expected_cwd = _canonical_absolute_path(_required_text(policy, "cwd"))
        expected_temp = _canonical_absolute_path(_required_text(policy, "temp_dir"))
        if cwd != expected_cwd or not cwd.is_relative_to(workspace):
            self._close_reservation(reservation)
            raise SandboxInitializationError("Broker launch cwd does not match the reservation")
        for name in ("TEMP", "TMP"):
            value = environment.get(name)
            if not isinstance(value, str) or _canonical_absolute_path(value) != expected_temp:
                self._close_reservation(reservation)
                raise SandboxInitializationError("Broker launch TEMP does not match the reservation")
        try:
            limits = ResourceLimits.from_mapping(
                policy.get("limits") if isinstance(policy.get("limits"), Mapping) else None
            )
            job = WindowsJobObject(f"mini-agent-{reservation.job_id}", limits)
        except Exception:
            self._close_reservation(reservation)
            raise
        try:
            process = _NativeWindowsProcess.launch(
                reservation.token,
                list(argv),
                str(cwd),
                {str(key): str(value) for key, value in environment.items()},
                job,
                logon_sid=reservation.logon_sid,
                service_sid=self.service_sid,
                desktop_name=reservation.desktop.startup_name,
            )
        except Exception:
            reservation.desktop.close()
            raise
        finally:
            _close_handle(reservation.token)
        process_id = f"process-{uuid.uuid4().hex}"
        lease = _NativeLease(process_id, backend_id, user_id, reservation.job_id, process, reservation.desktop)
        monitor = ResourceMonitor(
            process.pid,
            limits,
            provider=_NativeResourceProvider(process),
            on_exceeded=lambda error: self._resource_exceeded(process_id, error),
        )
        lease.resource_monitor = monitor
        with self._lock:
            self._processes[process_id] = lease
            self._jobs[(backend_id, user_id, reservation.job_id)] = process_id
        monitor.start()
        return {
            "accepted": True,
            "process_id": process_id,
            "pid": process.pid,
            "backend_instance_id": backend_id,
            "user_id": user_id,
            "job_id": reservation.job_id,
            "stdin": policy.get("stdin"),
            "stdout": policy.get("stdout"),
            "stderr": policy.get("stderr"),
            "resources": {"process_id": process_id, "pid": process.pid},
        }

    def control(self, operation: str, request: Mapping[str, Any]) -> Mapping[str, Any]:
        process_id = _required_text(request, "process_id")
        backend_id = _required_text(request, "backend_instance_id")
        with self._lock:
            lease = self._processes.get(process_id)
        if lease is None or lease.backend_instance_id != backend_id:
            raise SandboxInitializationError("Broker process is unavailable")
        process = lease.process
        if lease.failure_code is not None:
            raise SandboxResourceExceeded("Broker process exceeded a sandbox resource limit")
        if operation == "process_poll":
            return {"returncode": process.poll()}
        if operation == "process_wait":
            return {"returncode": process.wait(_timeout(request.get("timeout")))}
        if operation == "process_read":
            stream = str(request.get("stream") or "")
            if stream not in {"stdout", "stderr"}:
                raise SandboxInitializationError("Broker process stream is invalid")
            size = request.get("size", 65536)
            if isinstance(size, bool) or not isinstance(size, int) or not 1 <= size <= 1024 * 1024:
                raise SandboxInitializationError("Broker process read size is invalid")
            return {"data": b64encode(process.read(stream, size)).decode("ascii")}
        if operation == "process_write":
            value = request.get("data")
            if not isinstance(value, str):
                raise SandboxInitializationError("Broker process input is invalid")
            return {"written": process.write(b64decode(value.encode("ascii"), validate=True))}
        if operation == "process_close_stdin":
            process.close_stdin()
            return {"closed": True}
        if operation == "process_communicate":
            input_value = request.get("input")
            decoded = b64decode(input_value.encode("ascii"), validate=True) if isinstance(input_value, str) else None
            code, stdout, stderr = process.communicate(decoded, _timeout(request.get("timeout")))
            return {
                "returncode": code,
                "stdout": b64encode(stdout).decode("ascii"),
                "stderr": b64encode(stderr).decode("ascii"),
            }
        if operation in {"process_terminate", "process_kill"}:
            return {"returncode": process.terminate()}
        raise SandboxInitializationError("Broker process operation is unsupported")

    def release(self, request: Mapping[str, Any]) -> bool:
        backend_id = _required_text(request, "backend_instance_id")
        user_id = _required_text(request, "user_id")
        job_id = _required_text(request, "job_id")
        key = (backend_id, user_id, job_id)
        with self._lock:
            reservation_ids = [
                reservation_id
                for reservation_id, reservation in self._reservations.items()
                if (reservation.backend_instance_id, reservation.user_id, reservation.job_id) == key
            ]
            for reservation_id in reservation_ids:
                self._close_reservation(self._reservations.pop(reservation_id))
            process_id = self._jobs.pop(key, None)
            lease = self._processes.pop(process_id, None) if process_id else None
        if lease is None:
            return True
        if lease.resource_monitor is not None:
            lease.resource_monitor.stop()
        lease.process.close()
        lease.desktop.close()
        return True

    def reclaim(self, request: Mapping[str, Any]) -> tuple[str, ...]:
        current_backend = _required_text(request, "backend_instance_id")
        with self._lock:
            stale = [lease for lease in self._processes.values() if lease.backend_instance_id != current_backend]
            stale_reservations = [
                item for item in self._reservations.values() if item.backend_instance_id != current_backend
            ]
        for item in (*stale_reservations, *stale):
            self.release(
                {
                    "backend_instance_id": item.backend_instance_id,
                    "user_id": item.user_id,
                    "job_id": item.job_id,
                }
            )
        return tuple(lease.job_id for lease in stale)

    def recover(self, record) -> bool:
        pid = record.resources.get("pid")
        return not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0 or self._terminate_pid(pid)

    def _account(self, network_mode: NetworkMode) -> WindowsSandboxAccount:
        if network_mode is NetworkMode.FULL_NETWORK:
            return WindowsSandboxAccount(
                self.credentials.online_name,
                self.credentials.online_sid,
                self.credentials.online_password,
            )
        return WindowsSandboxAccount(
            self.credentials.offline_name,
            self.credentials.offline_sid,
            self.credentials.offline_password,
        )

    def _resource_exceeded(self, process_id: str, _error: Exception) -> None:
        with self._lock:
            lease = self._processes.get(process_id)
            if lease is None:
                return
            lease.failure_code = "resource_exceeded"
        lease.process.terminate()

    def _expire_loop(self) -> None:
        while not self._stop.wait(1.0):
            with self._lock:
                self._purge_expired_locked()

    def _purge_expired_locked(self) -> None:
        now = self._clock()
        expired = [key for key, value in self._reservations.items() if value.expires_at <= now]
        for key in expired:
            self._close_reservation(self._reservations.pop(key))

    @staticmethod
    def _close_reservation(reservation: _Reservation) -> None:
        _close_handle(reservation.token)
        reservation.desktop.close()

    @staticmethod
    def _terminate_pid(pid: int) -> bool:
        modules = _modules()
        try:
            handle = modules["api"].OpenProcess(0x0001 | 0x00100000, False, pid)
        except Exception as exc:
            return getattr(exc, "winerror", None) in {87, 1168}
        try:
            modules["process"].TerminateProcess(handle, 1)
            return True
        except Exception as exc:
            return getattr(exc, "winerror", None) in {87, 1168}
        finally:
            _close_handle(handle)


def _required_text(values: Mapping[str, Any], name: str) -> str:
    value = values.get(name)
    if not isinstance(value, str) or not value:
        raise SandboxInitializationError(f"Broker {name} is invalid")
    return value


def _timeout(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0 or value > 300:
        raise SandboxInitializationError("Broker process timeout is invalid")
    return float(value)


def _canonical_absolute_path(value: str) -> Path:
    candidate = Path(value)
    if not candidate.is_absolute():
        raise SandboxInitializationError("Broker launch path must be absolute")
    # The backend owns reparse-point and existence validation before it grants
    # the per-logon SID lease.  The service intentionally does not open user
    # workspaces under its own SID; it only repeats a lexical containment check
    # over the policy-hashed absolute paths.
    return Path(os.path.normcase(os.path.abspath(os.path.normpath(value))))


def _capability_digest(
    *,
    reservation_id: str,
    policy_hash: str,
    workspace: Path,
    temp_dir: Path,
    account_sid: str,
    workspace_cap_sid: str,
    temp_cap_sid: str,
    expires_at: float,
) -> str:
    value = {
        "reservation_id": reservation_id,
        "policy_hash": policy_hash,
        "workspace": str(workspace),
        "temp_dir": str(temp_dir),
        "account_sid": account_sid,
        "capability_sids": {"workspace": workspace_cap_sid, "temp": temp_cap_sid},
        "expires_at": f"{expires_at:.9f}",
    }
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    ).hexdigest()


def _policy_hash(policy: Mapping[str, Any]) -> str:
    payload = json.dumps(policy, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return hashlib.sha256(payload).hexdigest()


def _close_handle(handle: Any) -> None:
    try:
        handle.Close()
    except Exception:
        try:
            _modules()["api"].CloseHandle(handle)
        except Exception:
            pass


__all__ = ["WindowsNativeBrokerAdapter"]
