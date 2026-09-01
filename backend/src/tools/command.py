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
    format_command_output,
)
from backend.sandbox import (
    SandboxExecutionDecision,
    SandboxMaintenanceBusy,
    TerminalKind,
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
    ) -> None:
        self._workspace = workspace.resolve()
        self._is_windows = os.name == "nt" if is_windows is None else is_windows
        self._terminal_type = normalize_terminal_type(terminal_type)
        self._popen_factory = popen_factory
        self._tree_terminator = tree_terminator
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
        command_lease = None
        try:
            task_scope = parent_scope.child(
                JobScopeKind.TASK,
                parent_job_id=parent_scope.parent_job_id,
            )
            max_output_chars = self._MAX_OUTPUT_CHARS
            job_options: dict[str, Any] = {
                "max_output_chars": max_output_chars,
                "tree_terminator": self._tree_terminator,
                "is_windows": self._is_windows,
                "error_formatter": MessageErrorFormatter(),
            }
            if self._popen_factory is not None:
                job_options["popen_factory"] = self._popen_factory
            job_id = parent_scope.registry.new_job_id()
            effective_timeout = timeout_seconds
            decision = context.sandbox_decision
            if isinstance(decision, SandboxExecutionDecision):
                try:
                    command_lease = decision.launcher.command_lease()
                except SandboxMaintenanceBusy as exc:
                    raise ToolError("Sandbox Broker maintenance is in progress.") from exc
                if self._terminal_type == "wsl":
                    raise ToolError("WSL is disabled for sandboxed run_command execution.")
                policy = decision.command_policy(job_id, TerminalKind(self._terminal_type))
                max_output_chars = decision.limits.output_chars
                job_options["max_output_chars"] = max_output_chars
                effective_timeout = min(timeout_seconds, decision.limits.wall_seconds)
                sandbox_user_id = decision.user_id or "local"
                job_options["popen_factory"] = decision.launcher.popen_factory(
                    policy,
                    user_id=sandbox_user_id,
                    job_kind="command",
                )
                job_options["tree_terminator"] = decision.launcher.terminate_tree
                job_options["sandbox_policy"] = policy
                job_options["sandbox_launcher"] = decision.launcher
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
            return self._result(job, max_output_chars=max_output_chars)
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
            if command_lease is not None:
                command_lease.close()
            if private_registry is not None:
                private_registry.close_all(reason="private command registry closed", timeout=5.0)

    @staticmethod
    def _resolve_scope(context: ToolInvocationContext) -> tuple[JobScope, JobRegistry | None]:
        if context.job_scope is not None:
            if not isinstance(context.job_scope, JobScope):
                raise ToolError("The command process manager context is invalid.")
            return context.job_scope, None

        registry = JobRegistry()
        runner_scope = registry.root_scope().child(JobScopeKind.THREAD)
        run_scope = runner_scope.child(JobScopeKind.RUN, session_id=context.session_id)
        return run_scope, registry

    def _wait_for_job(self, job: SubprocessJob, context: ToolInvocationContext) -> None:
        cancel_sent = False
        while not job.wait(self._WAIT_INTERVAL_SECONDS):
            if not cancel_sent and context.cancel_requested is not None and context.cancel_requested():
                cancel_sent = job.cancel("tool invocation cancelled")

    @classmethod
    def _result(cls, job: SubprocessJob, *, max_output_chars: int) -> str:
        info = job.info()
        if info.state is JobState.SUCCEEDED:
            return cls._truncate_stdout(job.stdout, max_chars=max_output_chars)
        if info.state is JobState.CANCELLED:
            raise ToolError(cls._failure_output("Command was cancelled.", job, max_chars=max_output_chars))
        raise ToolError(cls._failure_output(info.error or "Command failed.", job, max_chars=max_output_chars))

    @staticmethod
    def _truncate_stdout(stdout: str, *, max_chars: int) -> str:
        if len(stdout) <= max_chars:
            return stdout

        marker = ""
        payload_chars = max_chars
        for _ in range(8):
            payload_chars = max(0, max_chars - len(marker))
            omitted = len(stdout) - payload_chars
            updated_marker = f"\n… output truncated ({omitted} characters omitted)"
            if updated_marker == marker:
                break
            marker = updated_marker
        return stdout[:payload_chars] + marker

    @staticmethod
    def _failure_output(status: str, job: SubprocessJob, *, max_chars: int) -> str:
        if not job.stdout and not job.stderr:
            return status
        output_budget = max(0, max_chars - len(status) - 1)
        output = format_command_output(job.stdout, job.stderr, max_chars=output_budget)
        return f"{status}\n{output}"

    def _command_line(self, command: str) -> list[str]:
        if not self._is_windows:
            return ["bash", "-c", command]

        executable = terminal_executable(self._terminal_type, environment=self._environment)
        if executable is None:
            raise ToolError(f"{TERMINAL_LABELS[self._terminal_type]} is not available on this system.")
        if self._terminal_type == "cmd":
            return [executable, "/d", "/s", "/c", command]
        if self._terminal_type in {"powershell", "pwsh"}:
            return [
                executable,
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-WorkingDirectory",
                str(self._workspace),
                "-Command",
                command,
            ]
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
