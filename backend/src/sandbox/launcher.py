"""Fail-closed process launcher for Windows sandbox jobs."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .admission import ResourceRequest, SandboxAdmission
from .broker import WindowsBrokerClient
from .errors import SandboxError, SandboxInitializationError, SandboxPolicyError
from .policy import (
    NetworkMode,
    PermissionMode,
    SandboxPolicy,
    ensure_disk_reserve,
    remove_temp_dir,
    resolve_network_rules,
)


class SandboxLauncher:
    """Launch untrusted processes through the Broker on Windows.

    ``allow_local_backend`` exists only for deterministic non-Windows unit
    tests and must be explicitly enabled. It is never selected by production
    code and is not a fallback when a Windows Broker request fails.
    """

    def __init__(
        self,
        *,
        broker: WindowsBrokerClient | None = None,
        is_windows: bool | None = None,
        allow_local_backend: bool = False,
        environment: Mapping[str, str] | None = None,
        admission: SandboxAdmission | None = None,
    ) -> None:
        self.broker = broker
        self.is_windows = os.name == "nt" if is_windows is None else is_windows
        self.allow_local_backend = allow_local_backend
        self.environment = dict(os.environ if environment is None else environment)
        self.admission = admission
        self._temp_dirs: dict[int, Path] = {}
        self._admitted: dict[int, tuple[str, ResourceRequest]] = {}
        self._job_ids: dict[int, str] = {}

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
    ) -> subprocess.Popen:
        if not argv or any(not isinstance(value, str) or not value for value in argv):
            raise SandboxPolicyError("argv must contain non-empty strings")
        policy.validate(is_windows=self.is_windows or self.allow_local_backend)
        ensure_disk_reserve(policy.workspace, required_bytes=policy.limits.disk_mib * 1024 * 1024)
        if self.is_windows and self.broker is None and not self.allow_local_backend:
            raise SandboxInitializationError("Windows Sandbox Broker is not initialized")
        if policy.network_mode is NetworkMode.RESTRICTED_NETWORK:
            # DNS resolution belongs outside the sandbox. The Broker receives
            # the host/port rules and performs the actual firewall operation.
            resolved = [
                {"address": rule.address, "port": rule.port} for rule in resolve_network_rules(policy.network_allowlist)
            ]
        else:
            resolved = []
        temp_dir = policy.create_temp_dir()
        env = policy.environment(self.environment if environment is None else environment, temp_dir=temp_dir)
        launch_cwd = str(cwd or policy.workspace)
        if (
            not _inside_workspace(Path(launch_cwd), policy.workspace)
            and policy.file_mode is not PermissionMode.FULL_ACCESS
        ):
            remove_temp_dir(temp_dir)
            raise SandboxPolicyError("job cwd is outside the workspace")
        request = ResourceRequest(
            memory_mib=policy.limits.memory_mib,
            processes=policy.limits.processes,
            handles=policy.limits.handles,
        )
        admitted = False
        if self.admission is not None:
            try:
                self.admission.acquire(policy.session_id, request)
            except Exception:
                remove_temp_dir(temp_dir)
                raise
            admitted = True
        try:
            if self.is_windows and (not self.allow_local_backend or self.broker is not None):
                if self.broker is None:
                    raise SandboxInitializationError("Windows Broker is not initialized")
                process = self.broker.launch(
                    argv=list(argv),
                    cwd=launch_cwd,
                    environment=env,
                    policy={
                        **policy.to_dict(),
                        "resolved_network": resolved,
                        "token_mode": "backend_user"
                        if policy.file_mode is PermissionMode.FULL_ACCESS
                        else "sandbox_account",
                        "stdin": "pipe" if stdin == subprocess.PIPE else "null",
                        "stdout": "pipe" if stdout == subprocess.PIPE else "null",
                        "stderr": "pipe" if stderr == subprocess.PIPE else "null",
                    },
                )
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
            remove_temp_dir(temp_dir)
            if admitted:
                self.admission.release(policy.session_id, request)
            if isinstance(exc, SandboxInitializationError):
                raise
            if isinstance(exc, SandboxError):
                raise
            raise SandboxInitializationError("sandbox process launch failed") from exc
        pid = getattr(process, "pid", None)
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
            remove_temp_dir(temp_dir)
            if admitted:
                self.admission.release(policy.session_id, request)
            raise SandboxInitializationError("sandbox process did not return a valid process id")
        self._temp_dirs[pid] = temp_dir
        self._job_ids[pid] = policy.job_id
        if admitted:
            self._admitted[pid] = (policy.session_id, request)
        return process

    def cleanup(self, process_or_pid: Any) -> bool:
        pid = process_or_pid if isinstance(process_or_pid, int) else getattr(process_or_pid, "pid", None)
        if not isinstance(pid, int):
            return True
        path = self._temp_dirs.pop(pid, None)
        cleaned = True if path is None else remove_temp_dir(path)
        job_id = self._job_ids.pop(pid, None)
        if job_id is not None and self.broker is not None:
            try:
                self.broker.release(job_id)
            except Exception:
                cleaned = False
        lease = self._admitted.pop(pid, None)
        if lease is not None and self.admission is not None:
            self.admission.release(*lease)
        return cleaned

    def popen_factory(self, policy: SandboxPolicy):
        """Adapt this launcher to the existing ``SubprocessJob`` port."""

        def factory(argv, **kwargs):
            return self.launch(
                argv,
                policy,
                cwd=kwargs.get("cwd"),
                environment=kwargs.get("env"),
                stdin=kwargs.get("stdin", subprocess.DEVNULL),
                stdout=kwargs.get("stdout"),
                stderr=kwargs.get("stderr"),
            )

        return factory


def _inside_workspace(candidate: Path, workspace: Path) -> bool:
    try:
        return candidate.resolve(strict=False).is_relative_to(workspace.resolve(strict=True))
    except (OSError, RuntimeError):
        return False


__all__ = ["SandboxLauncher"]
