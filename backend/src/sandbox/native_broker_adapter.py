"""Native process/account adapter hosted by the Windows Broker service."""

from __future__ import annotations

import hashlib
import subprocess
import threading
import time
import uuid
from base64 import b64decode, b64encode
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .broker_service import AccountLease, AccountPool
from .errors import SandboxInitializationError, SandboxResourceExceeded
from .native_windows import (
    WindowsAccountManager,
    WindowsAclManager,
    WindowsJobObject,
    WindowsRestrictedTokenFactory,
    WindowsSandboxAccount,
    _modules,
)
from .policy import FileAccessMode, NetworkMode, ResourceLimits, remove_temp_dir
from .resources import ResourceMonitor, ResourceUsage


class WfpController(Protocol):
    """Privileged WFP provider; rules are always fixed IP/port tuples."""

    def apply(
        self,
        *,
        rule_id: str,
        account_sid: str,
        mode: NetworkMode,
        endpoints: tuple[tuple[str, int], ...],
    ) -> tuple[str, ...]: ...

    def remove(self, rule_ids: tuple[str, ...]) -> bool: ...


class _NativeWindowsProcess:
    def __init__(
        self,
        *,
        process_handle: Any,
        thread_handle: Any,
        pid: int,
        stdin_handle: Any,
        stdout_handle: Any,
        stderr_handle: Any,
        job: WindowsJobObject,
    ) -> None:
        self.process_handle = process_handle
        self.thread_handle = thread_handle
        self.pid = pid
        self.stdin_handle = stdin_handle
        self.stdout_handle = stdout_handle
        self.stderr_handle = stderr_handle
        self.job = job
        self.returncode: int | None = None
        self._stdin_closed = False
        self._lock = threading.RLock()
        self._modules = _modules()
        self.output_bytes = 0
        self._reader_threads: tuple[threading.Thread, ...] = ()
        self._output_chunks: dict[str, list[bytes]] = {"stdout": [], "stderr": []}

    @classmethod
    def launch(
        cls,
        token: Any,
        argv: list[str],
        cwd: str,
        environment: Mapping[str, str],
        job: WindowsJobObject,
    ) -> _NativeWindowsProcess:
        modules = _modules()
        pipe = modules["pipe"]
        api = modules["api"]
        process = modules["process"]
        child_stdin, parent_stdin = pipe.CreatePipe(None, 0)
        parent_stdout, child_stdout = pipe.CreatePipe(None, 0)
        parent_stderr, child_stderr = pipe.CreatePipe(None, 0)
        for handle in (parent_stdin, parent_stdout, parent_stderr):
            api.SetHandleInformation(handle, modules["con"].HANDLE_FLAG_INHERIT, 0)
        startup = process.STARTUPINFO()
        startup.dwFlags |= modules["con"].STARTF_USESTDHANDLES
        startup.hStdInput = child_stdin
        startup.hStdOutput = child_stdout
        startup.hStdError = child_stderr
        flags = (
            modules["con"].CREATE_SUSPENDED
            | modules["con"].CREATE_NO_WINDOW
            | modules["con"].CREATE_UNICODE_ENVIRONMENT
        )
        try:
            process_handle, thread_handle, pid, _ = process.CreateProcessAsUser(
                token,
                None,
                subprocess.list2cmdline(argv),
                None,
                None,
                True,
                flags,
                dict(environment),
                cwd,
                startup,
            )
            job.assign(process_handle)
            process.ResumeThread(thread_handle)
        except Exception as exc:  # pragma: no cover - requires UAC
            job.terminate()
            job.close()
            raise SandboxInitializationError("Broker could not launch the restricted process") from exc
        finally:
            for handle in (child_stdin, child_stdout, child_stderr):
                try:
                    api.CloseHandle(handle)
                except Exception:
                    pass
        return cls(
            process_handle=process_handle,
            thread_handle=thread_handle,
            pid=int(pid),
            stdin_handle=parent_stdin,
            stdout_handle=parent_stdout,
            stderr_handle=parent_stderr,
            job=job,
        )

    def poll(self) -> int | None:
        with self._lock:
            if self.returncode is not None:
                return self.returncode
            result = self._modules["event"].WaitForSingleObject(self.process_handle, 0)
            if result == self._modules["con"].WAIT_TIMEOUT:
                return None
            self.returncode = int(self._modules["process"].GetExitCodeProcess(self.process_handle))
            return self.returncode

    def wait(self, timeout: float | None) -> int | None:
        milliseconds = self._modules["event"].INFINITE if timeout is None else max(0, int(timeout * 1000))
        result = self._modules["event"].WaitForSingleObject(self.process_handle, milliseconds)
        if result == self._modules["con"].WAIT_TIMEOUT:
            return None
        return self.poll()

    def read(self, stream: str, size: int) -> bytes:
        handle = self.stdout_handle if stream == "stdout" else self.stderr_handle
        try:
            _, value = self._modules["file"].ReadFile(handle, max(1, min(size, 1024 * 1024)))
            result = bytes(value)
            with self._lock:
                self.output_bytes += len(result)
            return result
        except Exception as exc:
            if getattr(exc, "winerror", None) in {109, 232}:
                return b""
            raise OSError("Broker process stream read failed") from exc

    def write(self, value: bytes) -> int:
        if self._stdin_closed:
            raise OSError("Broker process stdin is closed")
        try:
            _, written = self._modules["file"].WriteFile(self.stdin_handle, value)
            return int(written) if isinstance(written, int) else len(value)
        except Exception as exc:
            raise OSError("Broker process stream write failed") from exc

    def close_stdin(self) -> None:
        with self._lock:
            if self._stdin_closed:
                return
            self._stdin_closed = True
            self._modules["api"].CloseHandle(self.stdin_handle)

    def communicate(self, input_value: bytes | None, timeout: float | None) -> tuple[int | None, bytes, bytes]:
        if input_value:
            self.write(input_value)
        self.close_stdin()
        self._ensure_readers()
        code = self.wait(timeout)
        if code is not None:
            for thread in self._reader_threads:
                thread.join(timeout=5.0)
        with self._lock:
            return (
                code,
                b"".join(self._output_chunks["stdout"]),
                b"".join(self._output_chunks["stderr"]),
            )

    def _ensure_readers(self) -> None:
        with self._lock:
            if self._reader_threads:
                return

            def drain(stream: str) -> None:
                while True:
                    value = self.read(stream, 65536)
                    if not value:
                        return
                    with self._lock:
                        self._output_chunks[stream].append(value)

            self._reader_threads = tuple(
                threading.Thread(
                    target=drain,
                    args=(name,),
                    name=f"sandbox-{self.pid}-{name}",
                    daemon=True,
                )
                for name in ("stdout", "stderr")
            )
            for thread in self._reader_threads:
                thread.start()

    def terminate(self) -> int | None:
        self.job.terminate()
        return self.wait(5.0)

    def close(self) -> None:
        self.terminate()
        for thread in self._reader_threads:
            thread.join(timeout=5.0)
        for handle in (self.stdout_handle, self.stderr_handle, self.thread_handle, self.process_handle):
            try:
                self._modules["api"].CloseHandle(handle)
            except Exception:
                pass
        self.job.close()


@dataclass(slots=True)
class _NativeLease:
    process_id: str
    backend_instance_id: str
    user_id: str
    job_id: str
    process: _NativeWindowsProcess
    pool: AccountPool
    account_lease: AccountLease
    workspace: Path
    acl_snapshot: str
    temp_acl_snapshot: str
    wfp_rules: tuple[str, ...]
    temp_dir: Path
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
    """Broker adapter that owns accounts, ACLs, WFP rules and Job Objects."""

    def __init__(
        self,
        *,
        account_manager: WindowsAccountManager | None = None,
        acl_manager: WindowsAclManager | None = None,
        token_factory: WindowsRestrictedTokenFactory | None = None,
        wfp: WfpController | None = None,
    ) -> None:
        self.account_manager = account_manager or WindowsAccountManager()
        self.acl_manager = acl_manager or WindowsAclManager()
        self.token_factory = token_factory or WindowsRestrictedTokenFactory()
        self.wfp = wfp
        self._pools: dict[tuple[str, str], AccountPool] = {}
        self._accounts: dict[str, WindowsSandboxAccount] = {}
        self._processes: dict[str, _NativeLease] = {}
        self._jobs: dict[tuple[str, str, str], str] = {}
        self._lock = threading.RLock()

    def install(self) -> None:
        _modules()

    def repair(self) -> None:
        _modules()

    def launch(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        policy = request.get("policy")
        if not isinstance(policy, Mapping):
            raise SandboxInitializationError("Broker launch policy is invalid")
        backend_id = _required_text(request, "backend_instance_id")
        user_id = _required_text(request, "user_id")
        job_id = _required_text(policy, "job_id")
        job_kind = str(request.get("job_kind") or "command")
        if job_kind not in {"command", "mcp"}:
            raise SandboxInitializationError("Broker launch kind is invalid")
        argv = request.get("argv")
        environment = request.get("environment")
        if not isinstance(argv, list) or not argv or not all(isinstance(item, str) and item for item in argv):
            raise SandboxInitializationError("Broker launch argv is invalid")
        if not isinstance(environment, Mapping):
            raise SandboxInitializationError("Broker launch environment is invalid")
        workspace = Path(_required_text(policy, "workspace")).resolve(strict=True)
        cwd = Path(_required_text(request, "cwd")).resolve(strict=True)
        if not cwd.is_relative_to(workspace):
            raise SandboxInitializationError("Broker launch cwd is outside the workspace")
        limits = ResourceLimits.from_mapping(
            policy.get("limits") if isinstance(policy.get("limits"), Mapping) else None
        )
        file_mode = FileAccessMode(str(policy.get("file_mode") or FileAccessMode.READ_ONLY.value))
        network_mode = NetworkMode(str(policy.get("network_mode") or NetworkMode.NO_NETWORK.value))
        if file_mode is FileAccessMode.FULL_ACCESS:
            raise SandboxInitializationError("full_access must use the backend user token path")
        resolved = policy.get("resolved_network")
        endpoints = (
            tuple(
                (str(item.get("address")), int(item.get("port")))
                for item in resolved
                if isinstance(item, Mapping) and item.get("address") and item.get("port")
            )
            if isinstance(resolved, list)
            else ()
        )
        if network_mode is NetworkMode.RESTRICTED_NETWORK and not endpoints:
            raise SandboxInitializationError("restricted network endpoints are missing")
        if self.wfp is None:
            raise SandboxInitializationError("Broker WFP provider is unavailable")
        pool = self._pool(user_id, job_kind)
        account_lease = pool.acquire(job_id)
        account = self._accounts[account_lease.account]
        acl_snapshot = ""
        temp_acl_snapshot = ""
        wfp_rules: tuple[str, ...] = ()
        process: _NativeWindowsProcess | None = None
        temp_dir: Path | None = None
        try:
            acl_snapshot = self.acl_manager.protect(workspace, account.sid, file_mode)
            temp_dir = Path(str(environment.get("TEMP") or "")).resolve(strict=True)
            temp_acl_snapshot = self.acl_manager.protect(
                temp_dir,
                account.sid,
                FileAccessMode.WORKSPACE_WRITE,
            )
            rule_id = f"mini-agent-{hashlib.sha256(f'{backend_id}:{user_id}:{job_id}'.encode()).hexdigest()[:24]}"
            wfp_rules = self.wfp.apply(
                rule_id=rule_id,
                account_sid=account.sid,
                mode=network_mode,
                endpoints=endpoints,
            )
            job = WindowsJobObject(rule_id, limits)
            token = self.token_factory.create(account)
            try:
                process = _NativeWindowsProcess.launch(
                    token,
                    list(argv),
                    str(cwd),
                    {str(key): str(value) for key, value in environment.items()},
                    job,
                )
            finally:
                try:
                    token.Close()
                except Exception:
                    pass
            process_id = f"process-{uuid.uuid4().hex}"
            lease = _NativeLease(
                process_id,
                backend_id,
                user_id,
                job_id,
                process,
                pool,
                account_lease,
                workspace,
                acl_snapshot,
                temp_acl_snapshot,
                wfp_rules,
                temp_dir,
            )
            monitor = ResourceMonitor(
                process.pid,
                limits,
                provider=_NativeResourceProvider(process),
                on_exceeded=lambda error: self._resource_exceeded(process_id, error),
            )
            lease.resource_monitor = monitor
            with self._lock:
                self._processes[process_id] = lease
                self._jobs[(backend_id, user_id, job_id)] = process_id
            monitor.start()
            return {
                "accepted": True,
                "process_id": process_id,
                "pid": process.pid,
                "stdin": policy.get("stdin"),
                "stdout": policy.get("stdout"),
                "stderr": policy.get("stderr"),
                "resources": {
                    "process_id": process_id,
                    "pid": process.pid,
                    "account": account.name,
                    "sid": account.sid,
                    "wfp_rules": list(wfp_rules),
                    "temp_dir": str(temp_dir),
                    "workspace": str(workspace),
                    "acl_snapshot": acl_snapshot,
                    "temp_acl_snapshot": temp_acl_snapshot,
                },
            }
        except Exception:
            if process is not None:
                process.close()
            if wfp_rules:
                self.wfp.remove(wfp_rules)
            if acl_snapshot:
                self.acl_manager.restore(workspace, acl_snapshot)
            if temp_acl_snapshot and temp_dir is not None:
                self.acl_manager.restore(temp_dir, temp_acl_snapshot)
            try:
                pool.release(job_id, lambda _lease: True)
            except Exception:
                pass
            raise

    def control(self, operation: str, request: Mapping[str, Any]) -> Mapping[str, Any]:
        process_id = _required_text(request, "process_id")
        with self._lock:
            lease = self._processes.get(process_id)
        if lease is None:
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
        key = (
            _required_text(request, "backend_instance_id"),
            _required_text(request, "user_id"),
            _required_text(request, "job_id"),
        )
        with self._lock:
            process_id = self._jobs.get(key)
            lease = self._processes.get(process_id or "")
        if lease is None:
            return True
        complete = True
        if lease.resource_monitor is not None:
            lease.resource_monitor.stop()
        lease.process.close()
        complete = self.acl_manager.restore(lease.workspace, lease.acl_snapshot) and complete
        complete = self.wfp is not None and self.wfp.remove(lease.wfp_rules) and complete
        complete = self.acl_manager.restore(lease.temp_dir, lease.temp_acl_snapshot) and complete
        complete = remove_temp_dir(lease.temp_dir) and complete
        if not complete:
            return False
        lease.pool.release(lease.job_id, lambda _lease: True)
        with self._lock:
            self._jobs.pop(key, None)
            self._processes.pop(lease.process_id, None)
        return True

    def recover(self, record) -> bool:
        resources = record.resources
        complete = True
        pid = resources.get("pid")
        if isinstance(pid, int) and not isinstance(pid, bool) and pid > 0:
            complete = self._terminate_pid(pid) and complete
        raw_rules = resources.get("wfp_rules")
        rule_ids = tuple(str(value) for value in raw_rules) if isinstance(raw_rules, list) else ()
        complete = self.wfp is not None and self.wfp.remove(rule_ids) and complete
        workspace = resources.get("workspace")
        acl_snapshot = resources.get("acl_snapshot")
        if isinstance(workspace, str) and workspace and isinstance(acl_snapshot, str) and acl_snapshot:
            complete = self.acl_manager.restore(Path(workspace), acl_snapshot) and complete
        else:
            complete = False
        temp_dir = resources.get("temp_dir")
        if isinstance(temp_dir, str) and temp_dir:
            temp_acl_snapshot = resources.get("temp_acl_snapshot")
            if isinstance(temp_acl_snapshot, str) and temp_acl_snapshot:
                complete = self.acl_manager.restore(Path(temp_dir), temp_acl_snapshot) and complete
            else:
                complete = False
            complete = remove_temp_dir(Path(temp_dir)) and complete
        else:
            complete = False
        account = resources.get("account")
        if isinstance(account, str) and account:
            complete = self.account_manager.delete(account) and complete
        else:
            complete = False
        return complete

    @staticmethod
    def _terminate_pid(pid: int) -> bool:
        modules = _modules()
        try:
            handle = modules["api"].OpenProcess(0x0001 | 0x00100000, False, pid)
        except Exception as exc:
            if getattr(exc, "winerror", None) in {87, 1168}:
                return True
            return False
        try:
            modules["process"].TerminateProcess(handle, 1)
            return True
        except Exception as exc:
            return getattr(exc, "winerror", None) in {87, 1168}
        finally:
            try:
                modules["api"].CloseHandle(handle)
            except Exception:
                pass

    def _resource_exceeded(self, process_id: str, _error: Exception) -> None:
        with self._lock:
            lease = self._processes.get(process_id)
            if lease is None:
                return
            lease.failure_code = "resource_exceeded"
        lease.process.terminate()

    def _pool(self, user_id: str, kind: str) -> AccountPool:
        key = (user_id, kind)
        with self._lock:
            existing = self._pools.get(key)
            if existing is not None:
                return existing
            prefix = hashlib.sha256(user_id.encode("utf-8", errors="replace")).hexdigest()[:10]
            created: list[WindowsSandboxAccount] = []
            try:
                for index in range(4):
                    created.append(self.account_manager.create(f"ma{kind[0]}{prefix}{index}"))
            except Exception:
                for account in created:
                    self.account_manager.delete(account.name)
                raise
            accounts = tuple(created)
            pool = AccountPool(user_id, kind, tuple((account.name, account.sid) for account in accounts))
            for account in accounts:
                self._accounts[account.name] = account
            self._pools[key] = pool
            return pool


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


__all__ = ["WfpController", "WindowsNativeBrokerAdapter"]
