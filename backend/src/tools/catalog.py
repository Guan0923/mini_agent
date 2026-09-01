"""Default workspace tool assembly, kept outside the generic registry."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from backend.domain.terminal import DEFAULT_TERMINAL_TYPE, TerminalType

from .base import Tool
from .filesystem import WorkspaceFiles
from .registry import ToolRegistry
from .web import DdgrWebSearch, SafeWebFetcher
from .workspace_tools import build_workspace_tools


def build_tool_registry(
    workspace: Path,
    *,
    web_search: DdgrWebSearch | None = None,
    web_fetch: SafeWebFetcher | None = None,
    workspace_files: WorkspaceFiles | None = None,
    project_workspace: Path | None = None,
    terminal_type: TerminalType | str = DEFAULT_TERMINAL_TYPE,
    extra_tools: Iterable[Tool] = (),
) -> ToolRegistry:
    """Build the standard workspace tool registry."""

    return ToolRegistry(
        (
            *build_workspace_tools(
                workspace,
                web_search=web_search,
                web_fetch=web_fetch,
                workspace_files=workspace_files,
                project_workspace=project_workspace,
                terminal_type=terminal_type,
            ),
            *extra_tools,
        )
    )
