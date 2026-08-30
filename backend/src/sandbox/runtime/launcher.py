"""Fail-closed two-phase launcher for Windows run_command jobs."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import subprocess
import tempfile
import threading
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from ..control.broker import WindowsBrokerClient
from ..control.maintenance import SandboxCommandLease, SandboxMaintenanceGate
from ..errors import (
    SandboxError,
    SandboxInitializationError,
    SandboxPathError,
    SandboxPathFailure,
    SandboxPolicyError,
)
from ..native_windows import AclLeaseEntry, WindowsAclManager
from ..policy import (
    FileAccessMode,
    NetworkMode,
    SandboxJobContext,
    SandboxPolicy,
    ensure_disk_reserve,
    remove_temp_dir,
)
from .admission import ResourceRequest, SandboxAdmission
from .leases import CommandLease, CommandLeaseStore
from .proxy import ProxyCredential, RunCommandProxy


class _RollbackAction:
    def __init__(self, callback: Callable[[], object]) -> None:
        self.callback = callback
        self.active = True


class _RollbackStack:
    """One-shot reverse rollback stack which always attempts every action."""

    def __init__(self) -> None:
        self._actions: list[_RollbackAction] = []

    def push(self, callback: Callable[[], object]) -> _RollbackAction:
        action = _RollbackAction(callback)
        self._actions.append(action)
        return action

    def cancel(self, action: _RollbackAction) -> None:
        action.active = False

    def move_to_top(self, action: _RollbackAction) -> None:
        self._actions.remove(action)
        self._actions.append(action)

    def clear(self) -> None:
        for action in self._actions:
            action.active = False

    def run(self) -> bool:
        failures: list[Exception] = []
        for action in reversed(self._actions):
            if not action.active:
                continue
            action.active = False
            try:
                if action.callback() is False:
                    failures.append(RuntimeError("sandbox rollback action reported failure"))
            except Exception as exc:
                failures.append(exc)
        return not failures


class SandboxLauncher:
    """Reserve a fixed-account token, lease explicit paths, then launch."""

    _recovery_lock = threading.RLock()
    _broker_launch_lock = threading.RLock()
    _recovered_instances: set[tuple[str, str]] = set()

    def __init__(
        self,
        *,
        broker: WindowsBrokerClient | None = None,
        is_windows: bool | None = None,
        environment: Mapping[str, str] | None = None,
        admission: SandboxAdmission | None = None,
        acl_manager: WindowsAclManager | None = None,
        lease_store_path: Path | None = None,
        proxy_factory=None,
        maintenance_gate: SandboxMaintenanceGate | None = None,
    ) -> None:
        self.broker = broker
        self.is_windows = os.name == "nt" if is_windows is None else is_windows
        self.environment = dict(os.environ if environment is None else environment)
        self.admission = admission
        self.acl_manager = acl_manager or WindowsAclManager()
        default_lease_path = Path(tempfile.gettempdir()) / "mini-agent-sandbox" / "backend-leases-v1.json"
        self.lease_store = CommandLeaseStore(Path(lease_store_path or default_lease_path), self.acl_manager)
        self.proxy_factory = proxy_factory or RunCommandProxy.shared
        self.maintenance_gate = maintenance_gate or SandboxMaintenanceGate()
        self._temp_dirs: dict[int, Path] = {}
        self._admitted: dict[int, tuple[str, ResourceRequest]] = {}
        self._job_contexts: dict[int, SandboxJobContext] = {}
        self._leases: dict[int, CommandLease] = {}
        self._proxies: dict[int, RunCommandProxy] = {}
        self._maintenance_leases: dict[int, SandboxCommandLease] = {}

    def command_lease(self) -> SandboxCommandLease:
        return self.maintenance_gate.acquire_command()

    def launch(
        self,
        argv: Sequence[str],
        policy: SandboxPolicy,
        *,
        cwd: str | Path | None = None,
        environment: Mapping[str, str] | None = None,
        stdin: int | None = subprocess.DEVNULL,
        stdout: int | None = subprocess.PIPE,
        stderr: int | None = subprocess.PIPE,
        user_id: str = "local",
        job_kind: str = "command",
    ) -> subprocess.Popen:
        if job_kind != "command":
            raise SandboxPolicyError("Windows Sandbox Broker only accepts run_command jobs")
        if not argv or any(not isinstance(value, str) or not value for value in argv):
            raise SandboxPolicyError("argv must contain non-empty strings")
        policy.validate(is_windows=self.is_windows)
        if not self.is_windows:
            raise SandboxInitializationError("Windows sandbox is unavailable on this platform")
        if self.broker is None:
            raise SandboxInitializationError("Windows Sandbox Broker is not initialized")

        workspace_paths = tuple(
            self._explicit_directory(path, SandboxPathFailure.WORKSPACE_INVALID) for path in policy.workspaces
        )
        launch_cwd_path = self._explicit_directory(cwd or workspace_paths[0], SandboxPathFailure.CWD_INVALID)
        if not _inside_any_workspace(launch_cwd_path, workspace_paths):
            raise SandboxPathError(SandboxPathFailure.CWD_OUTSIDE_WORKSPACE, launch_cwd_path)
        for workspace in workspace_paths:
            ensure_disk_reserve(workspace)

        temp_dir = policy.create_temp_dir()
        try:
            temp_dir = self._explicit_directory(temp_dir, SandboxPathFailure.TEMP_INVALID)
            ensure_disk_reserve(temp_dir)
        except Exception:
            remove_temp_dir(temp_dir)
            raise
        launch_cwd = str(launch_cwd_path)
        env = policy.environment(self.environment if environment is None else environment, temp_dir=temp_dir)
        job_context = SandboxJobContext(user_id=user_id, policy=policy, job_kind="command")
        request = ResourceRequest(
            memory_mib=policy.limits.memory_mib,
            processes=policy.limits.processes,
            handles=policy.limits.handles,
        )

        rollback = _RollbackStack()
        temp_action = rollback.push(lambda: remove_temp_dir(temp_dir))
        maintenance_lease: SandboxCommandLease | None = None
        admitted = False
        lease: CommandLease | None = None
        proxy: RunCommandProxy | None = None
        acl_entries: list[AclLeaseEntry] = []
        acl_actions: list[_RollbackAction] = []
        broker_action: _RollbackAction | None = None
        try:
            maintenance_lease = self.command_lease()
            rollback.push(maintenance_lease.close)
            if self.admission is not None:
                self.admission.acquire(job_context.user_id, request)
                admitted = True
                rollback.push(lambda: self.admission.release(job_context.user_id, request))
            self._recover_once()
            policy_payload = {
                **policy.to_dict(),
                "workspaces": [str(path) for path in workspace_paths],
                "cwd": launch_cwd,
                "temp_dir": str(temp_dir),
                "stdin": "pipe" if stdin == subprocess.PIPE else "null",
                "stdout": "pipe" if stdout == subprocess.PIPE else "null",
                "stderr": "pipe" if stderr == subprocess.PIPE else "null",
            }
            policy_hash = _mapping_hash(policy_payload)
            reservation = self.broker.reserve(
                policy=policy_payload,
                policy_hash=policy_hash,
                user_id=job_context.user_id,
            )
            broker_action = rollback.push(lambda: self.broker.release(policy.job_id, user_id=job_context.user_id))
            reservation_id = str(reservation["reservation_id"])
            logon_sid = str(reservation["logon_sid"])
            account_sid = str(reservation["account_sid"])
            service_sid = str(reservation["service_sid"])
            capability_sids = reservation["capability_sids"]
            if not isinstance(capability_sids, Mapping):
                raise SandboxInitializationError("Broker capability SID response is invalid")
            workspace_cap_sid = str(capability_sids["workspace"])
            temp_cap_sid = str(capability_sids["temp"])
            capability_digest = str(reservation["capability_digest"])

            explicit_paths = _unique_paths((*workspace_paths, launch_cwd_path))
            identities = self._inspect_explicit_paths((*explicit_paths, temp_dir))
            for path in explicit_paths:
                acl_entries.append(
                    self._apply(lambda: self.acl_manager.grant_lease(path, account_sid, policy.file_mode), path)
                )
                acl_actions.append(rollback.push(lambda entry=acl_entries[-1]: self.acl_manager.revoke_entry(entry)))
                if policy.file_mode is FileAccessMode.READ_ONLY:
                    acl_entries.append(
                        self._apply(lambda: self.acl_manager.deny_capability_write(path, workspace_cap_sid), path)
                    )
                    acl_actions.append(
                        rollback.push(lambda entry=acl_entries[-1]: self.acl_manager.revoke_entry(entry))
                    )
                elif policy.file_mode is FileAccessMode.WORKSPACE_WRITE:
                    acl_entries.append(
                        self._apply(lambda: self.acl_manager.grant_capability_write(path, workspace_cap_sid), path)
                    )
                    acl_actions.append(
                        rollback.push(lambda entry=acl_entries[-1]: self.acl_manager.revoke_entry(entry))
                    )
            acl_entries.append(
                self._apply(
                    lambda: self.acl_manager.grant_lease(temp_dir, account_sid, FileAccessMode.WORKSPACE_WRITE),
                    temp_dir,
                )
            )
            acl_actions.append(rollback.push(lambda entry=acl_entries[-1]: self.acl_manager.revoke_entry(entry)))
            if policy.file_mode is not FileAccessMode.FULL_ACCESS:
                acl_entries.append(
                    self._apply(lambda: self.acl_manager.grant_capability_write(temp_dir, temp_cap_sid), temp_dir)
                )
                acl_actions.append(rollback.push(lambda entry=acl_entries[-1]: self.acl_manager.revoke_entry(entry)))
            self._verify_identities(identities)

            lease = CommandLease(
                policy.job_id,
                reservation_id,
                logon_sid,
                account_sid,
                service_sid,
                tuple(str(workspace) for workspace in workspace_paths),
                launch_cwd,
                str(temp_dir),
                policy.file_mode.value,
                workspace_cap_sid,
                temp_cap_sid,
                capability_digest,
                tuple(acl_entries),
            )
            self.lease_store.add(lease)
            for action in acl_actions:
                rollback.cancel(action)
            rollback.cancel(temp_action)
            rollback.push(lambda: self.lease_store.release(lease))
            proxy = self._configure_proxy(policy, env)
            if proxy is not None:
                rollback.push(lambda: proxy.revoke_job(policy.job_id))
            rollback.move_to_top(broker_action)
            process = self._broker_launch(
                argv=list(argv),
                cwd=launch_cwd,
                environment=env,
                reservation_id=reservation_id,
                policy_hash=policy_hash,
                capability_digest=capability_digest,
                user_id=job_context.user_id,
                service_sid=service_sid,
                execute_paths=explicit_paths,
            )
        except Exception as exc:
            if not rollback.run():
                raise SandboxPathError(SandboxPathFailure.CLEANUP_FAILED, temp_dir) from exc
            if isinstance(exc, SandboxError):
                raise
            raise SandboxInitializationError("sandbox process launch failed") from exc

        pid = getattr(process, "pid", None)
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
            if not rollback.run():
                raise SandboxPathError(SandboxPathFailure.CLEANUP_FAILED, temp_dir)
            raise SandboxInitializationError("sandbox process did not return a valid process id")
        rollback.clear()
        self._temp_dirs[pid] = temp_dir
        self._maintenance_leases[pid] = maintenance_lease
        self._job_contexts[pid] = job_context
        self._leases[pid] = lease
        if proxy is not None:
            self._proxies[pid] = proxy
        if admitted:
            self._admitted[pid] = (job_context.user_id, request)
        return process

    def _broker_launch(self, *, service_sid: str, execute_paths: tuple[Path, ...], **kwargs: Any) -> Any:
        entries: list[AclLeaseEntry] = []
        with self._broker_launch_lock:
            try:
                for path in execute_paths:
                    entries.append(self._apply(lambda: self.acl_manager.grant_execute_lease(path, service_sid), path))
                return self.broker.launch(**kwargs)
            finally:
                if not self._revoke_entries(entries):
                    raise SandboxPathError(SandboxPathFailure.CLEANUP_FAILED, execute_paths[0])

    def _explicit_directory(self, path: str | Path, reason: SandboxPathFailure) -> Path:
        absolute = Path(os.path.abspath(os.fspath(path)))
        try:
            resolved = absolute.resolve(strict=True)
            if not resolved.is_dir():
                raise OSError("not a directory")
        except Exception as exc:
            raise SandboxPathError(reason, absolute) from exc
        try:
            return self.acl_manager.inspect_dacl(resolved).path
        except Exception as exc:
            raise SandboxPathError(SandboxPathFailure.DACL_READ_FAILED, resolved) from exc

    def _inspect_explicit_paths(self, paths: tuple[Path, ...]) -> dict[str, str]:
        identities: dict[str, str] = {}
        for path in _unique_paths(paths):
            try:
                identity = self.acl_manager.inspect_dacl(path)
            except Exception as exc:
                raise SandboxPathError(SandboxPathFailure.DACL_READ_FAILED, path) from exc
            identities[str(identity.path)] = identity.object_id
        return identities

    def _verify_identities(self, identities: Mapping[str, str]) -> None:
        for path, expected in identities.items():
            try:
                current = self.acl_manager.inspect_dacl(Path(path))
            except Exception as exc:
                raise SandboxPathError(SandboxPathFailure.DACL_VERIFY_FAILED, path) from exc
            if current.object_id != expected or os.path.normcase(str(current.path)) != os.path.normcase(path):
                raise SandboxPathError(SandboxPathFailure.PATH_IDENTITY_CHANGED, path)

    def _apply(self, operation, path: Path) -> AclLeaseEntry:
        try:
            entry = operation()
        except Exception as exc:
            raise SandboxPathError(SandboxPathFailure.DACL_APPLY_FAILED, path) from exc
        if not isinstance(entry, AclLeaseEntry) or not self.acl_manager.verify_entry(entry):
            raise SandboxPathError(SandboxPathFailure.DACL_VERIFY_FAILED, path)
        return entry

    def _revoke_entries(self, entries: Sequence[AclLeaseEntry]) -> bool:
        cleaned = True
        for entry in reversed(entries):
            try:
                cleaned = self.acl_manager.revoke_entry(entry) and cleaned
            except Exception:
                cleaned = False
        return cleaned

    def cleanup(self, process_or_pid: Any) -> bool:
        pid = process_or_pid if isinstance(process_or_pid, int) else getattr(process_or_pid, "pid", None)
        if not isinstance(pid, int):
            return True
        path = self._temp_dirs.get(pid)
        job_context = self._job_contexts.get(pid)
        lease = self._leases.get(pid)
        proxy = self._proxies.get(pid)
        maintenance_lease = self._maintenance_leases.get(pid)
        cleaned = True
        broker_released = True
        if job_context is not None:
            try:
                self.broker.release(job_context.policy.job_id, user_id=job_context.user_id)
            except Exception:
                cleaned = False
                broker_released = False
        if broker_released:
            if proxy is not None and job_context is not None:
                try:
                    proxy.revoke_job(job_context.policy.job_id)
                except Exception:
                    cleaned = False
            if lease is not None:
                try:
                    cleaned = self.lease_store.release(lease) and cleaned
                except Exception:
                    cleaned = False
                path = None
            if path is not None:
                cleaned = remove_temp_dir(path) and cleaned
            admitted = self._admitted.get(pid)
            if admitted is not None and self.admission is not None:
                try:
                    self.admission.release(*admitted)
                except Exception:
                    cleaned = False
                else:
                    self._admitted.pop(pid, None)
        if maintenance_lease is not None:
            try:
                maintenance_lease.close()
            except Exception:
                cleaned = False
            else:
                self._maintenance_leases.pop(pid, None)
        if cleaned:
            self._temp_dirs.pop(pid, None)
            self._job_contexts.pop(pid, None)
            self._leases.pop(pid, None)
            self._proxies.pop(pid, None)
        return cleaned

    def popen_factory(self, policy: SandboxPolicy, *, user_id: str = "local", job_kind: str = "command"):
        def factory(argv, **kwargs):
            return self.launch(
                argv,
                policy,
                cwd=kwargs.get("cwd"),
                environment=kwargs.get("env"),
                stdin=kwargs.get("stdin", subprocess.DEVNULL),
                stdout=kwargs.get("stdout"),
                stderr=kwargs.get("stderr"),
                user_id=user_id,
                job_kind=job_kind,
            )

        return factory

    @staticmethod
    def terminate_tree(process: Any) -> None:
        process.terminate()

    def _configure_proxy(self, policy: SandboxPolicy, env: dict[str, str]) -> RunCommandProxy | None:
        if policy.network_mode is NetworkMode.FULL_NETWORK:
            return None
        proxy = self.proxy_factory(policy.proxy_port)
        if policy.network_mode is NetworkMode.RESTRICTED_NETWORK:
            credential = proxy.issue(
                policy.job_id,
                policy.network_allowlist,
                ttl_seconds=min(3600, policy.limits.wall_seconds + 30),
            )
        else:
            credential = ProxyCredential(f"disabled-{secrets.token_urlsafe(8)}", secrets.token_urlsafe(24))
        proxy_url = credential.url(policy.proxy_port)
        env.update(
            {
                "HTTP_PROXY": proxy_url,
                "HTTPS_PROXY": proxy_url,
                "ALL_PROXY": proxy_url,
                "http_proxy": proxy_url,
                "https_proxy": proxy_url,
                "all_proxy": proxy_url,
                "NO_PROXY": "",
                "no_proxy": "",
            }
        )
        return proxy

    def _recover_once(self) -> None:
        key = (self.broker.backend_instance_id, str(self.lease_store.path.resolve()))
        with self._recovery_lock:
            if key in self._recovered_instances:
                return
            self.broker.reclaim_stale()
            self.lease_store.recover()
            self._recovered_instances.add(key)


def _inside_any_workspace(candidate: Path, workspaces: tuple[Path, ...]) -> bool:
    try:
        return any(candidate == workspace or candidate.is_relative_to(workspace) for workspace in workspaces)
    except (OSError, RuntimeError, ValueError):
        return False


def _unique_paths(paths: Sequence[Path]) -> tuple[Path, ...]:
    result: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        key = os.path.normcase(str(path))
        if key not in seen:
            seen.add(key)
            result.append(path)
    return tuple(result)


def _mapping_hash(value: Mapping[str, object]) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return hashlib.sha256(payload).hexdigest()


__all__ = ["SandboxLauncher"]
