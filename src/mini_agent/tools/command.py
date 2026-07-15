"""Workspace-rooted cross-platform command execution."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .base import ToolError

CommandRunner = Callable[..., subprocess.CompletedProcess[str]]


class WorkspaceCommand:
    """Run an explicitly approved command with the workspace as its working directory."""

    _MAX_TIMEOUT_SECONDS = 120
    _MAX_OUTPUT_CHARS = 20_000

    def __init__(
        self,
        workspace: Path,
        *,
        is_windows: bool | None = None,
        runner: CommandRunner = subprocess.run,
    ) -> None:
        self._workspace = workspace.resolve()
        self._is_windows = os.name == "nt" if is_windows is None else is_windows
        self._runner = runner

    def run(self, command: str, timeout_seconds: int = 30) -> str:
        """Execute Bash on Unix-like systems and PowerShell on Windows."""
        self._validate(command, timeout_seconds)
        try:
            result = self._runner(
                self._command_line(command),
                cwd=self._workspace,
                capture_output=True,
                check=False,
                encoding="utf-8",
                errors="replace",
                text=True,
                timeout=timeout_seconds,
            )
        except FileNotFoundError as exc:
            shell = "PowerShell" if self._is_windows else "Bash"
            raise ToolError(f"{shell} is not available on this system.") from exc
        except subprocess.TimeoutExpired as exc:
            output = self._format_output(exc.stdout, exc.stderr)
            suffix = f"\n{output}" if output else ""
            raise ToolError(f"Command timed out after {timeout_seconds} seconds.{suffix}") from exc
        except OSError as exc:
            raise ToolError(f"Unable to start command: {exc}") from exc

        output = self._format_output(result.stdout, result.stderr)
        if result.returncode != 0:
            suffix = f"\n{output}" if output else ""
            raise ToolError(f"Command exited with code {result.returncode}.{suffix}")
        return output or "Command completed successfully."

    def _command_line(self, command: str) -> list[str]:
        if self._is_windows:
            return ["powershell", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command]
        return ["bash", "-c", command]

    def _format_output(self, stdout: str | bytes | None, stderr: str | bytes | None) -> str:
        parts: list[str] = []
        if stdout:
            parts.append(f"stdout:\n{self._truncate(self._as_text(stdout))}")
        if stderr:
            parts.append(f"stderr:\n{self._truncate(self._as_text(stderr))}")
        return "\n".join(parts)

    def _truncate(self, value: str) -> str:
        if len(value) <= self._MAX_OUTPUT_CHARS:
            return value
        omitted = len(value) - self._MAX_OUTPUT_CHARS
        return f"{value[:self._MAX_OUTPUT_CHARS]}\n… output truncated ({omitted} characters omitted)"

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
