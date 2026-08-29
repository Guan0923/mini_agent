"""Definition for the workspace command tool."""

from __future__ import annotations

from ..base import Tool
from ..command import WorkspaceCommand
from .schema import object_schema


def command_tool(commands: WorkspaceCommand) -> Tool:
    return Tool(
        "run_command",
        (
            "Executes a general Bash command on Unix-like systems or PowerShell command on Windows from "
            "the workspace. Use read_file, glob, grep, write_file, or edit_file for ordinary file work. "
            "Use this fallback for tests, builds, Git, scripts, computation, and operations without a "
            "dedicated tool. Commands may modify files or access paths outside the workspace and therefore "
            "require approval. Output is limited to 20,000 characters; timeout_seconds is at most 120."
        ),
        commands.run,
        object_schema(
            {
                "command": {
                    "type": "string",
                    "description": "Shell command to execute. Use platform-appropriate syntax.",
                },
                "timeout_seconds": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 120,
                    "default": 30,
                    "description": "Maximum seconds before the command is forcibly terminated.",
                },
            },
            ["command"],
        ),
        requires_confirmation=True,
        read_only=False,
        context_handler=commands.run_with_context,
    )
