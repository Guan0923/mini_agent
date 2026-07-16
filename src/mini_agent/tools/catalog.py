"""Default workspace tool assembly, kept outside the generic registry."""

from __future__ import annotations

from pathlib import Path

from .base import Tool
from .calculator import calculate
from .command import WorkspaceCommand
from .filesystem import WorkspaceFiles
from .registry import ToolRegistry
from .web import DdgrWebSearch, SafeWebFetcher


def _object_schema(
    properties: dict[str, dict[str, object]],
    required: list[str] | None = None,
) -> dict[str, object]:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": False,
    }


def build_workspace_tools(
    workspace: Path,
    *,
    web_search: DdgrWebSearch | None = None,
    web_fetch: SafeWebFetcher | None = None,
) -> tuple[Tool, ...]:
    """Create the standard tool set for one workspace without owning registry policy."""

    files = WorkspaceFiles(workspace)
    commands = WorkspaceCommand(workspace)
    search = web_search or DdgrWebSearch()
    fetcher = web_fetch or SafeWebFetcher()
    return (
        Tool(
            "calculator",
            "Safely evaluates basic arithmetic.",
            calculate,
            _object_schema({"expression": {"type": "string"}}, ["expression"]),
            retryable=True,
        ),
        Tool(
            "list_files",
            "Lists files below the workspace.",
            files.list_files,
            _object_schema({"path": {"type": "string", "default": "."}}),
            retryable=True,
        ),
        Tool(
            "read_file",
            "Reads a UTF-8 text file from the workspace.",
            files.read_file,
            _object_schema({"path": {"type": "string"}}, ["path"]),
            retryable=True,
        ),
        Tool(
            "web_search",
            "Searches the public web through DuckDuckGo and returns compact results.",
            search.search,
            _object_schema(
                {
                    "query": {"type": "string"},
                    "max_results": {"type": "integer", "minimum": 1, "maximum": 10, "default": 5},
                },
                ["query"],
            ),
            requires_confirmation=True,
            retryable=True,
        ),
        Tool(
            "web_fetch",
            "Fetches readable text from a public web URL with network safety checks.",
            fetcher.fetch,
            _object_schema(
                {
                    "url": {"type": "string"},
                    "max_chars": {"type": "integer", "minimum": 1, "maximum": 100_000, "default": 50_000},
                },
                ["url"],
            ),
            requires_confirmation=True,
            retryable=True,
        ),
        Tool(
            "write_file",
            "Writes a UTF-8 text file in the workspace.",
            files.write_file,
            _object_schema(
                {"path": {"type": "string"}, "content": {"type": "string"}},
                ["path", "content"],
            ),
            requires_confirmation=True,
            read_only=False,
        ),
        Tool(
            "delete_file",
            "Deletes one workspace file.",
            files.delete_file,
            _object_schema({"path": {"type": "string"}}, ["path"]),
            requires_confirmation=True,
            read_only=False,
            retryable=False,
        ),
        Tool(
            "delete_folder",
            "Deletes an empty workspace folder, or all contents when recursive is true.",
            files.delete_folder,
            _object_schema(
                {"path": {"type": "string"}, "recursive": {"type": "boolean", "default": False}},
                ["path"],
            ),
            requires_confirmation=True,
            read_only=False,
            retryable=False,
        ),
        Tool(
            "move_file",
            "Moves one workspace file to a new path.",
            files.move_file,
            _object_schema(
                {"source": {"type": "string"}, "destination": {"type": "string"}},
                ["source", "destination"],
            ),
            requires_confirmation=True,
            read_only=False,
            retryable=False,
        ),
        Tool(
            "move_folder",
            "Moves one workspace folder to a new path.",
            files.move_folder,
            _object_schema(
                {"source": {"type": "string"}, "destination": {"type": "string"}},
                ["source", "destination"],
            ),
            requires_confirmation=True,
            read_only=False,
            retryable=False,
        ),
        Tool(
            "run_command",
            "Runs an approved Bash command on Unix-like systems or PowerShell command on Windows from the workspace.",
            commands.run,
            _object_schema(
                {
                    "command": {"type": "string"},
                    "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 120, "default": 30},
                },
                ["command"],
            ),
            requires_confirmation=True,
            read_only=False,
        ),
    )


def build_workspace_tool_registry(
    workspace: Path,
    *,
    web_search: DdgrWebSearch | None = None,
    web_fetch: SafeWebFetcher | None = None,
) -> ToolRegistry:
    """Build the standard registry at the composition boundary."""

    return ToolRegistry(build_workspace_tools(workspace, web_search=web_search, web_fetch=web_fetch))
