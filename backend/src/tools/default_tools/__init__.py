"""Factories for the standard workspace tool catalog."""

from __future__ import annotations

from pathlib import Path

from backend.domain.terminal import DEFAULT_TERMINAL_TYPE, TerminalType

from ..base import Tool
from ..command import WorkspaceCommand
from ..filesystem import WorkspaceFiles
from ..web import DdgrWebSearch, SafeWebFetcher
from .command import command_tool
from .filesystem import filesystem_mutation_tools, filesystem_read_tools, upload_file_read_tool
from .time import time_tools
from .todo import todo_tools
from .web import web_tools


def build_default_tools(
    workspace: Path,
    *,
    files: WorkspaceFiles | None = None,
    search: DdgrWebSearch | None = None,
    fetcher: SafeWebFetcher | None = None,
    upload_files: WorkspaceFiles | None = None,
    terminal_type: TerminalType | str = DEFAULT_TERMINAL_TYPE,
    sandbox_config: object | None = None,
    network_mode: str | None = None,
) -> tuple[Tool, ...]:
    """Build tools in the stable order exposed to planners."""

    workspace_files = files or WorkspaceFiles(workspace)
    del sandbox_config
    tools = [
        *time_tools(),
        *todo_tools(),
        *filesystem_read_tools(workspace_files),
        *web_tools(
            search or DdgrWebSearch(network_mode=network_mode),
            fetcher or SafeWebFetcher(network_mode=network_mode),
        ),
        *filesystem_mutation_tools(workspace_files),
        command_tool(
            WorkspaceCommand(
                workspace,
                terminal_type=terminal_type,
            )
        ),
    ]
    if upload_files is not None:
        tools.append(upload_file_read_tool(upload_files))
    return tuple(tools)


__all__ = ["build_default_tools"]
