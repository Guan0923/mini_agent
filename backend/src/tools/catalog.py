"""Default workspace tool assembly, kept outside the generic registry."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from backend.domain.terminal import DEFAULT_TERMINAL_TYPE, TerminalType

from .base import Tool
from .default_tools import build_default_tools
from .filesystem import WorkspaceFiles
from .registry import ToolRegistry
from .web import DdgrWebSearch, SafeWebFetcher


def _build_tools(
    workspace: Path,
    *,
    web_search: DdgrWebSearch | None = None,
    web_fetch: SafeWebFetcher | None = None,
    workspace_files: WorkspaceFiles | None = None,
    upload_files: WorkspaceFiles | None = None,
    terminal_type: TerminalType | str = DEFAULT_TERMINAL_TYPE,
    rag_tool: Tool | None = None,
) -> tuple[Tool, ...]:
    """Create the standard tool set for one workspace."""

    return build_default_tools(
        workspace,
        files=workspace_files,
        search=web_search,
        fetcher=web_fetch,
        upload_files=upload_files,
        terminal_type=terminal_type,
        rag_tool=rag_tool,
    )


def build_tool_registry(
    workspace: Path,
    *,
    web_search: DdgrWebSearch | None = None,
    web_fetch: SafeWebFetcher | None = None,
    workspace_files: WorkspaceFiles | None = None,
    upload_files: WorkspaceFiles | None = None,
    terminal_type: TerminalType | str = DEFAULT_TERMINAL_TYPE,
    extra_tools: Iterable[Tool] = (),
    rag_tool: Tool | None = None,
) -> ToolRegistry:
    """Build the standard workspace tool registry."""

    return ToolRegistry(
        (
            *_build_tools(
                workspace,
                web_search=web_search,
                web_fetch=web_fetch,
                workspace_files=workspace_files,
                upload_files=upload_files,
                terminal_type=terminal_type,
                rag_tool=rag_tool,
            ),
            *extra_tools,
        )
    )
