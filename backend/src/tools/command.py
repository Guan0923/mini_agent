"""Workspace-rooted cross-platform command execution."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from backend.domain.terminal import DEFAULT_TERMINAL_TYPE, TERMINAL_LABELS, TerminalType, normalize_terminal_type
from backend.jobs import (
    AdmissionPolicy,
    JobAdmissionRejected,
    JobAdmissionTimeout,
    JobLane,
    JobQueueFull,
    JobRegistrationError,
    JobRegistry,
    JobScope,
    JobScopeClosed,
    JobScopeKind,
    JobState,
    MessageErrorFormatter,
    ProcessFactory,
    SlotMode,
    SubprocessJob,
    TreeTerminator,
)
from backend.sandbox import (
    NetworkMode,
    NetworkRule,
    PermissionMode,
    SandboxLauncher,
    SandboxLimits,
    SandboxPolicy,
    TerminalKind,
    normalize_permission_mode,
)

from .base import ToolError, ToolInvocationContext
from .terminal import terminal_executable, windows_workspace_to_wsl


class WorkspaceCommand:
    """Run an explicitly approved command with the workspace as its working directory."""

    _MAX_TIMEOUT_SECONDS = 120
    _MAX_OUTPUT_CHARS = 20_000
    _WAIT_INTERVAL_SECONDS = 0.05
    _SENSITIVE_ENV_COMPOUNDS = (
        "ACCESS_KEY",
        "API_KEY",
        "PRIVATE_KEY",
    )
    _SENSITIVE_ENV_SEGMENTS = {
        "AUTH",
        "AUTHORIZATION",
        "COOKIE",
        "CREDENTIAL",
        "CREDENTIALS",
        "PASSWD",
        "PASSWORD",
        "PAT",
        "SECRET",
        "TOKEN",
    }

    def __init__(
        self,
        workspace: Path,
        *,
        is_windows: bool | None = None,
        terminal_type: TerminalType | str = DEFAULT_TERMINAL_TYPE,
        popen_factory: ProcessFactory | None = None,
        tree_terminator: TreeTerminator | None = None,
        environment: Mapping[str, str] | None = None,
        sandbox_launcher: SandboxLauncher | None = None,
        sandbox_config: Mapping[str, object] | None = None,
    ) -> None:
        self._workspace = workspace.resolve()
        self._is_windows = os.name == "nt" if is_windows is None else is_windows
        self._terminal_type = normalize_terminal_type(terminal_type)
        self._popen_factory = popen_factory
        self._tree_terminator = tree_terminator
        self._sandbox_launcher = sandbox_launcher
        self._sandbox_config = dict(sandbox_config or {})
        self._environment = self._filtered_environment(os.environ if environment is None else environment)

    def run(self, command: str, timeout_seconds: int = 30) -> str:
        """Execute through a private registry when no runtime context exists."""

        return self.run_with_context(ToolInvocationContext(), command, timeout_seconds)

    def run_with_context(
        self,
        context: ToolInvocationContext,
        command: str,
        timeout_seconds: int = 30,
    ) -> str:
        """Execute as a managed subprocess job in the invocation's registry."""

        self._validate(command, timeout_seconds)
        if context.cancel_requested is not None and context.cancel_requested():
            raise ToolError("Command was cancelled before start.")

        parent_scope, private_registry = self._resolve_scope(context)
        job: SubprocessJob | None = None
        try:
            task_scope = parent_scope.child(
                JobScopeKind.TASK,
                parent_job_id=parent_scope.parent_job_id,
            )
            job_options: dict[str, Any] = {
                "max_output_chars": self._MAX_OUTPUT_CHARS,
                "tree_terminator": self._tree_terminator,
                "is_windows": self._is_windows,
                "error_formatter": MessageErrorFormatter(),
            }
            if self._popen_factory is not None:
                job_options["popen_factory"] = self._popen_factory
            job_id = parent_scope.registry.new_job_id()
            effective_timeout = timeout_seconds
            if self._sandbox_launcher is not None and bool(self._sandbox_config.get("enabled", True)):
                configured_file_mode = PermissionMode(
                    str(self._sandbox_config.get("file_mode", PermissionMode.READ_ONLY.value))
                )
                requested_file_mode = context.permission_mode
                file_mode = (
                    normalize_permission_mode(requested_file_mode)
                    if requested_file_mode is not None
                    else configured_file_mode
                )
                network_mode = NetworkMode(str(self._sandbox_config.get("network_mode", NetworkMode.NO_NETWORK.value)))
                raw_rules = self._sandbox_config.get("network_allowlist")
                network_allowlist = (
                    tuple(
                        NetworkRule(str(item.get("host") or ""), int(item.get("port")))
                        for item in raw_rules
                        if isinstance(item, Mapping)
                    )
                    if isinstance(raw_rules, (list, tuple))
                    else ()
                )
                limits = SandboxLimits.from_mapping(
                    self._sandbox_config.get("limits")
                    if isinstance(self._sandbox_config.get("limits"), Mapping)
                    else None
                )
                if file_mode is PermissionMode.FULL_ACCESS:
                    # Full access is a joint file/network decision. The
                    # frontend confirmation is represented by the explicit
                    # runtime mode and never persisted in the sandbox config.
                    network_mode = NetworkMode.FULL_NETWORK
                policy = SandboxPolicy(
                    workspace=self._workspace,
                    session_id=context.session_id or "runtime",
                    job_id=job_id,
                    file_mode=file_mode,
                    network_mode=network_mode,
                    network_allowlist=network_allowlist,
                    limits=limits,
                    terminal=TerminalKind(self._terminal_type),
                    enforced=file_mode is not PermissionMode.FULL_ACCESS,
                    full_access_acknowledged=file_mode is PermissionMode.FULL_ACCESS,
                )
                job_options["max_output_chars"] = limits.output_chars
                effective_timeout = min(timeout_seconds, limits.wall_seconds)
                job_options["popen_factory"] = self._sandbox_launcher.popen_factory(policy)
                job_options["sandbox_policy"] = policy
                job_options["sandbox_launcher"] = self._sandbox_launcher
            job = SubprocessJob(
                job_id,
                self._command_line(command),
                self._environment,
                str(self._workspace),
                effective_timeout,
                **job_options,
            )
            task_scope.submit(
                job,
                lane=JobLane.FOREGROUND,
                admission=AdmissionPolicy(slot_mode=SlotMode.INHERIT),
            )
            self._wait_for_job(job, context)
            return self._result(job)
        except FileNotFoundError as exc:
            shell = TERMINAL_LABELS.get(self._terminal_type, "Bash") if self._is_windows else "Bash"
            raise ToolError(f"{shell} is not available on this system.") from exc
        except OSError as exc:
            raise ToolError(f"Unable to start command: {exc}") from exc
        except (JobAdmissionRejected, JobAdmissionTimeout, JobQueueFull) as exc:
            raise ToolError("Command could not be admitted by the process manager.") from exc
        except (JobRegistrationError, JobScopeClosed, ValueError) as exc:
            raise ToolError("Command could not be registered with the process manager.") from exc
        finally:
            if private_registry is not None:
                private_registry.close_all(reason="private command registry closed", timeout=5.0)

    @staticmethod
    def _resolve_scope(context: ToolInvocationContext) -> tuple[JobScope, JobRegistry | None]:
        if context.job_scope is not None:
            if not isinstance(context.job_scope, JobScope):
                raise ToolError("The command process manager context is invalid.")
            return context.job_scope, None

        registry = JobRegistry()
        runner_scope = registry.root_scope().child(JobScopeKind.RUNNER)
        run_scope = runner_scope.child(JobScopeKind.RUN, session_id=context.session_id)
        return run_scope, registry

    def _wait_for_job(self, job: SubprocessJob, context: ToolInvocationContext) -> None:
        cancel_sent = False
        while not job.wait(self._WAIT_INTERVAL_SECONDS):
            if not cancel_sent and context.cancel_requested is not None and context.cancel_requested():
                cancel_sent = job.cancel("tool invocation cancelled")

    @staticmethod
    def _result(job: SubprocessJob) -> str:
        info = job.info()
        output = job.output
        if info.state is JobState.SUCCEEDED:
            return output or "Command completed successfully."
        if info.state is JobState.CANCELLED:
            suffix = f"\n{output}" if output else ""
            raise ToolError(f"Command was cancelled.{suffix}")
        error = info.error or "Command failed."
        suffix = f"\n{output}" if output else ""
        raise ToolError(f"{error}{suffix}")

    def _command_line(self, command: str) -> list[str]:
        if not self._is_windows:
            return ["bash", "-c", command]

        executable = terminal_executable(self._terminal_type, environment=self._environment)
        if executable is None:
            raise ToolError(f"{TERMINAL_LABELS[self._terminal_type]} is not available on this system.")
        if self._terminal_type == "cmd":
            return [executable, "/d", "/s", "/c", command]
        if self._terminal_type in {"powershell", "pwsh"}:
            return [executable, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command]
        if self._terminal_type == "git_bash":
            return [executable, "-lc", command]
        try:
            linux_workspace = windows_workspace_to_wsl(self._workspace)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
        return [executable, "--cd", linux_workspace, "--", "sh", "-lc", command]

    def _validate(self, command: Any, timeout_seconds: Any) -> None:
        if not isinstance(command, str) or not command.strip():
            raise ToolError("command must be a non-empty string.")
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, int):
            raise ToolError("timeout_seconds must be an integer.")
        if not 1 <= timeout_seconds <= self._MAX_TIMEOUT_SECONDS:
            raise ToolError(f"timeout_seconds must be between 1 and {self._MAX_TIMEOUT_SECONDS}.")

    @classmethod
    def _filtered_environment(cls, environment: Mapping[str, str]) -> dict[str, str]:
        return {name: value for name, value in environment.items() if not cls._is_sensitive_environment_name(name)}

    @classmethod
    def _is_sensitive_environment_name(cls, name: str) -> bool:
        normalised = name.upper()
        if any(compound in normalised for compound in cls._SENSITIVE_ENV_COMPOUNDS):
            return True
        return any(segment in cls._SENSITIVE_ENV_SEGMENTS for segment in normalised.split("_"))
