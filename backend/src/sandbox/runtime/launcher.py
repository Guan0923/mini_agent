"""Fail-closed two-phase launcher for Windows run_command jobs."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import subprocess
import tempfile
import threading
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from ..control.broker import WindowsBrokerClient
from ..errors import SandboxError, SandboxInitializationError, SandboxPolicyError
from ..native_windows import WindowsAclManager
from ..policy import (
    FileAccessMode,
    NetworkMode,
    SandboxJobContext,
    SandboxPolicy,
    ensure_disk_reserve,
    remove_temp_dir,
)
from .admission import ResourceRequest, SandboxAdmission
from .audit import SandboxAuditError, SandboxAuditFailure, WorldWritablePathAuditor, verify_audit_identities
from .leases import CommandLease, CommandLeaseStore
from .proxy import ProxyCredential, RunCommandProxy


class SandboxLauncher:
    """Reserve a fixed-account token, apply backend leases, then launch."""

    _recovery_lock = threading.RLock()
    _broker_launch_lock = threading.RLock()
    _recovered_instances: set[tuple[str, str]] = set()

    def __init__(
        self,
        *,
        broker: WindowsBrokerClient | None = None,
        is_windows: bool | None = None,
        allow_local_backend: bool = False,
        environment: Mapping[str, str] | None = None,
        admission: SandboxAdmission | None = None,
        acl_manager: WindowsAclManager | None = None,
        lease_store_path: Path | None = None,
        proxy_factory=None,
        path_auditor: WorldWritablePathAuditor | None = None,
    ) -> None:
        self.broker = broker
        self.is_windows = os.name == "nt" if is_windows is None else is_windows
        self.allow_local_backend = allow_local_backend
        self.environment = dict(os.environ if environment is None else environment)
        self.admission = admission
        self.acl_manager = acl_manager or WindowsAclManager()
        default_lease_path = Path(tempfile.gettempdir()) / "mini-agent-sandbox" / "backend-leases.json"
        self.lease_store = CommandLeaseStore(Path(lease_store_path or default_lease_path), self.acl_manager)
        self.proxy_factory = proxy_factory or RunCommandProxy.shared
        self.path_auditor = path_auditor or WorldWritablePathAuditor(self.acl_manager)
        self._temp_dirs: dict[int, Path] = {}
        self._admitted: dict[int, tuple[str, ResourceRequest]] = {}
        self._job_contexts: dict[int, SandboxJobContext] = {}
        self._leases: dict[int, CommandLease] = {}
        self._proxies: dict[int, RunCommandProxy] = {}

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
        policy.validate(is_windows=self.is_windows or self.allow_local_backend)
        job_context = SandboxJobContext(user_id=user_id, policy=policy, job_kind="command")
        for workspace in policy.workspaces:
            ensure_disk_reserve(workspace, required_bytes=policy.limits.disk_mib * 1024 * 1024)
        if self.is_windows and self.broker is None and not self.allow_local_backend:
            raise SandboxInitializationError("Windows Sandbox Broker is not initialized")
        launch_cwd_path = Path(cwd or policy.workspaces[0]).resolve(strict=True)
        launch_cwd = str(launch_cwd_path)
        if not _inside_any_workspace(launch_cwd_path, policy.workspaces):
            raise SandboxPolicyError("job cwd is outside the workspaces")
        temp_dir = policy.create_temp_dir()
        env = policy.environment(self.environment if environment is None else environment, temp_dir=temp_dir)
        request = ResourceRequest(
            memory_mib=policy.limits.memory_mib,
            processes=policy.limits.processes,
            handles=policy.limits.handles,
        )
        admitted = False
        if self.admission is not None:
            try:
                self.admission.acquire(job_context.user_id, request)
            except Exception:
                remove_temp_dir(temp_dir)
                raise
            admitted = True
        lease: CommandLease | None = None
        proxy: RunCommandProxy | None = None
        try:
            if self.is_windows and (not self.allow_local_backend or self.broker is not None):
                if self.broker is None:
                    raise SandboxInitializationError("Windows Broker is not initialized")
                self._recover_once()
                policy_payload = {
                    **policy.to_dict(),
                    "cwd": launch_cwd,
                    "temp_dir": str(temp_dir.resolve(strict=True)),
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
                audit = self.path_auditor.scan(
                    workspaces=policy.workspaces,
                    temp_dir=temp_dir,
                    environment=self.environment,
                    account_sid=account_sid,
                    file_mode=policy.file_mode,
                )
                lease = CommandLease(
                    policy.job_id,
                    reservation_id,
                    logon_sid,
                    account_sid,
                    service_sid,
                    tuple(str(workspace) for workspace in policy.workspaces),
                    str(temp_dir),
                    policy.file_mode.value,
                    workspace_cap_sid,
                    temp_cap_sid,
                    capability_digest,
                    tuple(str(path) for path in audit.deny_paths),
                    dict(audit.identities),
                )
                self.lease_store.add(lease)
                verify_audit_identities(self.acl_manager, audit.identities)
                for deny_path in audit.deny_paths:
                    try:
                        self.acl_manager.deny_capability_write(deny_path, workspace_cap_sid)
                        self.acl_manager.deny_capability_write(deny_path, temp_cap_sid)
                    except SandboxInitializationError as exc:
                        raise SandboxAuditError(
                            SandboxAuditFailure.CAPABILITY_ACL_FAILED,
                            risk_paths=(str(deny_path),),
                        ) from exc
                    if not all(
                        self.acl_manager.capability_write_denied(deny_path, sid)
                        for sid in (workspace_cap_sid, temp_cap_sid)
                    ):
                        raise SandboxAuditError(
                            SandboxAuditFailure.PATH_UNPROTECTED,
                            risk_paths=(str(deny_path),),
                        )
                for workspace in policy.workspaces:
                    self.acl_manager.grant_lease(workspace, account_sid, policy.file_mode)
                    if policy.file_mode is FileAccessMode.WORKSPACE_WRITE:
                        self.acl_manager.grant_capability_write(workspace, workspace_cap_sid)
                self.acl_manager.grant_lease(temp_dir, account_sid, FileAccessMode.WORKSPACE_WRITE)
                if policy.file_mode is not FileAccessMode.FULL_ACCESS:
                    self.acl_manager.grant_capability_write(temp_dir, temp_cap_sid)
                verify_audit_identities(self.acl_manager, audit.identities)
                proxy = self._configure_proxy(policy, env)
                with self._broker_launch_lock:
                    for workspace in policy.workspaces:
                        self.acl_manager.grant_execute_lease(workspace, service_sid)
                    try:
                        process = self.broker.launch(
                            argv=list(argv),
                            cwd=launch_cwd,
                            environment=env,
                            reservation_id=reservation_id,
                            policy_hash=policy_hash,
                            capability_digest=capability_digest,
                            user_id=job_context.user_id,
                        )
                    finally:
                        for workspace in policy.workspaces:
                            self.acl_manager.revoke_lease(workspace, service_sid)
            else:
                process = subprocess.Popen(
                    list(argv),
                    cwd=launch_cwd,
                    env=env,
                    stdin=stdin,
                    stdout=stdout,
                    stderr=stderr,
                    creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if self.is_windows else 0,
                    start_new_session=not self.is_windows,
                )
        except Exception as exc:
            if self.broker is not None:
                try:
                    self.broker.release(policy.job_id, user_id=job_context.user_id)
                except Exception:
                    pass
            if proxy is not None:
                proxy.revoke_job(policy.job_id)
            if lease is not None:
                self.lease_store.release(lease)
            else:
                remove_temp_dir(temp_dir)
            if admitted and self.admission is not None:
                self.admission.release(job_context.user_id, request)
            if isinstance(exc, SandboxError):
                raise
            raise SandboxInitializationError("sandbox process launch failed") from exc
        pid = getattr(process, "pid", None)
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
            remove_temp_dir(temp_dir)
            if admitted and self.admission is not None:
                self.admission.release(job_context.user_id, request)
            raise SandboxInitializationError("sandbox process did not return a valid process id")
        self._temp_dirs[pid] = temp_dir
        self._job_contexts[pid] = job_context
        if lease is not None:
            self._leases[pid] = lease
        if proxy is not None:
            self._proxies[pid] = proxy
        if admitted:
            self._admitted[pid] = (job_context.user_id, request)
        return process

    def cleanup(self, process_or_pid: Any) -> bool:
        pid = process_or_pid if isinstance(process_or_pid, int) else getattr(process_or_pid, "pid", None)
        if not isinstance(pid, int):
            return True
        path = self._temp_dirs.pop(pid, None)
        job_context = self._job_contexts.pop(pid, None)
        lease = self._leases.pop(pid, None)
        proxy = self._proxies.pop(pid, None)
        cleaned = True
        if job_context is not None and self.broker is not None and lease is not None:
            try:
                self.broker.release(job_context.policy.job_id, user_id=job_context.user_id)
            except Exception:
                cleaned = False
            if proxy is not None:
                proxy.revoke_job(job_context.policy.job_id)
            cleaned = self.lease_store.release(lease) and cleaned
            path = None
        if path is not None:
            cleaned = remove_temp_dir(path) and cleaned
        admitted = self._admitted.pop(pid, None)
        if admitted is not None and self.admission is not None:
            self.admission.release(*admitted)
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
        if self.broker is None:
            return
        key = (self.broker.backend_instance_id, str(self.lease_store.path.resolve()))
        with self._recovery_lock:
            if key in self._recovered_instances:
                return
            self.broker.reclaim_stale()
            self.lease_store.recover()
            self._recovered_instances.add(key)


def _inside_any_workspace(candidate: Path, workspaces: tuple[Path, ...]) -> bool:
    try:
        resolved = candidate.resolve(strict=False)
        return any(resolved.is_relative_to(workspace.resolve(strict=True)) for workspace in workspaces)
    except (OSError, RuntimeError):
        return False


def _mapping_hash(value: Mapping[str, object]) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return hashlib.sha256(payload).hexdigest()


__all__ = ["SandboxLauncher"]
