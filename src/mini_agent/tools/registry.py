"""Registration and invocation policy for all local tools."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .base import ConfirmationRequired, Tool, ToolError
from .calculator import calculate
from .command import WorkspaceCommand
from .filesystem import WorkspaceFiles


class ToolRegistry:
    def __init__(self, workspace: Path) -> None:
        files = WorkspaceFiles(workspace)
        commands = WorkspaceCommand(workspace)
        self._tools: dict[str, Tool] = {
            "calculator": Tool("calculator", "Safely evaluates basic arithmetic.", calculate),
            "list_files": Tool("list_files", "Lists files below the workspace.", files.list_files),
            "read_file": Tool("read_file", "Reads a UTF-8 text file from the workspace.", files.read_file),
            "write_file": Tool(
                "write_file",
                "Writes a UTF-8 text file in the workspace.",
                files.write_file,
                requires_confirmation=True,
                read_only=False,
            ),
            "run_command": Tool(
                "run_command",
                "Runs an approved Bash command on Unix-like systems or PowerShell command on Windows from the workspace.",
                commands.run,
                requires_confirmation=True,
                read_only=False,
            ),
        }

    def names(self) -> list[str]:
        return list(self._tools)

    def read_only_names(self) -> list[str]:
        return [name for name, tool in self._tools.items() if tool.read_only]

    def is_read_only(self, name: str) -> bool:
        tool = self._tools.get(name)
        if tool is None:
            raise ToolError(f"Unknown tool: {name}")
        return tool.read_only

    def invoke(self, name: str, arguments: dict[str, Any], confirmed: bool = False) -> str:
        tool = self._tools.get(name)
        if tool is None:
            raise ToolError(f"Unknown tool: {name}")
        if tool.requires_confirmation and not confirmed:
            raise ConfirmationRequired(f"{name} requires confirmation before it performs a potentially destructive operation.")
        try:
            return tool.handler(**arguments)
        except ToolError:
            raise
        except (OSError, TypeError, ValueError) as exc:
            raise ToolError(str(exc)) from exc
