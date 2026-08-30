"""Definition for the workspace command tool."""

from __future__ import annotations

from ..base import Tool
from ..command import WorkspaceCommand
from .schema import object_schema


def command_tool(commands: WorkspaceCommand) -> Tool:
    return Tool(
        "run_command",
        "Executes a shell command from the workspace using the configured terminal and returns its output.",
        commands.run,
        object_schema(
            {
                "command": {
                    "type": "string",
                    "description": ("The shell command to execute using syntax supported by the configured terminal."),
                },
                "timeout_seconds": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 120,
                    "default": 30,
                    "description": (
                        "The maximum number of seconds the command may run, from 1 to 120. Defaults to 30."
                    ),
                },
            },
            ["command"],
        ),
        requires_confirmation=True,
        read_only=False,
        workspace_confined=True,
        context_handler=commands.run_with_context,
    )
