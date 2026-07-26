"""Workspace-rooted cross-platform command execution."""

from __future__ import annotations

import os
import signal
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from .base import ToolError

ProcessFactory = Callable[..., subprocess.Popen[str]]
TreeTerminator = Callable[[subprocess.Popen[str]], None]


class WorkspaceCommand:
    """Run an explicitly approved command with the workspace as its working directory."""

    _MAX_TIMEOUT_SECONDS = 120
    _MAX_OUTPUT_CHARS = 20_000
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
        popen_factory: ProcessFactory = subprocess.Popen,
        tree_terminator: TreeTerminator | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self._workspace = workspace.resolve()
        self._is_windows = os.name == "nt" if is_windows is None else is_windows
        self._popen_factory = popen_factory
        self._tree_terminator = tree_terminator
        self._environment = self._filtered_environment(os.environ if environment is None else environment)

    def run(self, command: str, timeout_seconds: int = 30) -> str:
        """Execute Bash on Unix-like systems and PowerShell on Windows."""

        self._validate(command, timeout_seconds)
        process_options: dict[str, Any] = {
            "cwd": self._workspace,
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "encoding": "utf-8",
            "errors": "replace",
            "text": True,
            "env": self._environment,
        }
        if self._is_windows:
            process_options["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        else:
            process_options["start_new_session"] = True

        try:
            process = self._popen_factory(self._command_line(command), **process_options)
        except FileNotFoundError as exc:
            shell = "PowerShell" if self._is_windows else "Bash"
            raise ToolError(f"{shell} is not available on this system.") from exc
        except OSError as exc:
            raise ToolError(f"Unable to start command: {exc}") from exc

        try:
            stdout, stderr = process.communicate(timeout=timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            self._terminate_process_tree(process)
            try:
                stdout, stderr = process.communicate()
            except subprocess.TimeoutExpired:
                process.kill()
                stdout, stderr = process.communicate()
            output = self._format_output(stdout, stderr)
            suffix = f"\n{output}" if output else ""
            raise ToolError(f"Command timed out after {timeout_seconds} seconds.{suffix}") from exc
        except OSError as exc:
            self._terminate_process_tree(process)
            raise ToolError(f"Unable to communicate with command: {exc}") from exc

        output = self._format_output(stdout, stderr)
        if process.returncode != 0:
            suffix = f"\n{output}" if output else ""
            raise ToolError(f"Command exited with code {process.returncode}.{suffix}")
        return output or "Command completed successfully."

    def _command_line(self, command: str) -> list[str]:
        if self._is_windows:
            return ["powershell", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command]
        return ["bash", "-c", command]

    def _terminate_process_tree(self, process: subprocess.Popen[str]) -> None:
        if self._tree_terminator is not None:
            self._tree_terminator(process)
            return

        if self._is_windows:
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                    timeout=5,
                    env=self._environment,
                )
            except (OSError, subprocess.TimeoutExpired):
                pass
        else:
            try:
                killpg = getattr(os, "killpg")
                killpg(process.pid, signal.SIGKILL)
            except (AttributeError, OSError, ProcessLookupError):
                pass

        try:
            if process.poll() is None:
                process.kill()
        except OSError:
            pass

    def _format_output(self, stdout: str | bytes | None, stderr: str | bytes | None) -> str:
        streams: list[tuple[str, str]] = []
        if stdout:
            streams.append(("stdout", self._as_text(stdout)))
        if stderr:
            streams.append(("stderr", self._as_text(stderr)))
        if not streams:
            return ""

        complete = "\n".join(f"{label}:\n{value}" for label, value in streams)
        if len(complete) <= self._MAX_OUTPUT_CHARS:
            return complete

        marker = ""
        allocations = [0] * len(streams)
        for _ in range(8):
            fixed_chars = sum(len(label) + 2 for label, _value in streams) + len(streams) - 1 + len(marker)
            payload_budget = max(0, self._MAX_OUTPUT_CHARS - fixed_chars)
            allocations = self._allocate_payload([len(value) for _label, value in streams], payload_budget)
            omitted = sum(len(value) - allocation for (_label, value), allocation in zip(streams, allocations))
            updated_marker = f"\n… output truncated ({omitted} characters omitted)"
            if updated_marker == marker:
                break
            marker = updated_marker

        parts = [f"{label}:\n{value[:allocation]}" for (label, value), allocation in zip(streams, allocations)]
        return "\n".join(parts) + marker

    @staticmethod
    def _allocate_payload(lengths: list[int], budget: int) -> list[int]:
        allocations = [min(length, budget // len(lengths)) for length in lengths]
        remaining = budget - sum(allocations)
        for index, length in enumerate(lengths):
            extra = min(length - allocations[index], remaining)
            allocations[index] += extra
            remaining -= extra
            if remaining == 0:
                break
        return allocations

    @staticmethod
    def _as_text(value: str | bytes) -> str:
        return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value

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
