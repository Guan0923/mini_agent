"""Factories for the standard workspace tool catalog."""

from __future__ import annotations

from pathlib import Path

from ..base import Tool
from ..command import WorkspaceCommand
from ..filesystem import WorkspaceFiles
from ..web import DdgrWebSearch, SafeWebFetcher
from .command import command_tool
from .filesystem import filesystem_mutation_tools, filesystem_read_tools
from .time import time_tools
from .web import web_tools


def build_default_tools(
    workspace: Path,
    *,
    files: WorkspaceFiles | None = None,
    search: DdgrWebSearch | None = None,
    fetcher: SafeWebFetcher | None = None,
) -> tuple[Tool, ...]:
    """Build tools in the stable order exposed to planners."""

    workspace_files = files or WorkspaceFiles(workspace)
    return (
        *time_tools(),
        *filesystem_read_tools(workspace_files),
        *web_tools(search or DdgrWebSearch(), fetcher or SafeWebFetcher()),
        *filesystem_mutation_tools(workspace_files),
        command_tool(WorkspaceCommand(workspace)),
    )


__all__ = ["build_default_tools"]
