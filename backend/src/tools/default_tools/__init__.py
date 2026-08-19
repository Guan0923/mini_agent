"""Factories for the standard workspace tool catalog."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from backend.domain.terminal import DEFAULT_TERMINAL_TYPE, TerminalType
from backend.sandbox import SandboxLauncher

from ..base import Tool
from ..command import WorkspaceCommand
from ..filesystem import WorkspaceFiles
from ..read_pdf import read_pdf_tool
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
    rag_tool: Tool | None = None,
    sandbox_launcher: SandboxLauncher | None = None,
    sandbox_config: Mapping[str, object] | None = None,
    network_mode: str | None = None,
) -> tuple[Tool, ...]:
    """Build tools in the stable order exposed to planners."""

    workspace_files = files or WorkspaceFiles(workspace)
    configured_network_mode = network_mode
    configured_allowlist: tuple[tuple[str, int], ...] = ()
    if configured_network_mode is None and isinstance(sandbox_config, Mapping):
        raw_mode = sandbox_config.get("network_mode")
        configured_network_mode = str(raw_mode) if raw_mode is not None else None
        raw_rules = sandbox_config.get("network_allowlist")
        if isinstance(raw_rules, (list, tuple)):
            configured_allowlist = tuple(
                (str(item.get("host")), int(item.get("port")))
                for item in raw_rules
                if isinstance(item, Mapping) and item.get("host") and item.get("port")
            )
    tools = [
        *time_tools(),
        *todo_tools(),
        *filesystem_read_tools(workspace_files),
        read_pdf_tool(workspace),
        *web_tools(
            search
            or DdgrWebSearch(
                network_mode=configured_network_mode,
                network_allowlist=configured_allowlist,
            ),
            fetcher
            or SafeWebFetcher(
                allow_private_network=configured_network_mode == "full_network",
                network_mode=configured_network_mode,
                network_allowlist=configured_allowlist,
            ),
        ),
        *filesystem_mutation_tools(workspace_files),
        command_tool(
            WorkspaceCommand(
                workspace,
                terminal_type=terminal_type,
                sandbox_launcher=sandbox_launcher,
                sandbox_config=sandbox_config,
            )
        ),
    ]
    if upload_files is not None:
        tools.append(upload_file_read_tool(upload_files))
    if rag_tool is not None:
        tools.append(rag_tool)
    return tuple(tools)


__all__ = ["build_default_tools"]
