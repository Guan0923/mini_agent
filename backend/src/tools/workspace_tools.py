"""Construction of the concrete tool set for one workspace."""

from __future__ import annotations

from pathlib import Path

from backend.domain.terminal import DEFAULT_TERMINAL_TYPE, TerminalType

from .base import Tool
from .default_tools import build_default_tools
from .filesystem import WorkspaceFiles
from .web import DdgrWebSearch, SafeWebFetcher


def build_workspace_tools(
    workspace: Path,
    *,
    web_search: DdgrWebSearch | None = None,
    web_fetch: SafeWebFetcher | None = None,
    workspace_files: WorkspaceFiles | None = None,
    project_workspace: Path | None = None,
    terminal_type: TerminalType | str = DEFAULT_TERMINAL_TYPE,
) -> tuple[Tool, ...]:
    """Create the standard concrete tool set without constructing a registry."""

    return build_default_tools(
        workspace,
        files=workspace_files,
        search=web_search,
        fetcher=web_fetch,
        project_workspace=project_workspace,
        terminal_type=terminal_type,
    )
