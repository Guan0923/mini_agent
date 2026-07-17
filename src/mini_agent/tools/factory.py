"""Composition helpers for the default workspace tool set."""

from __future__ import annotations

from pathlib import Path

from .defaults import build_default_tools
from .registry import ToolRegistry
from .web import DdgrWebSearch, SafeWebFetcher


def build_tool_registry(
    workspace: Path,
    *,
    web_search: DdgrWebSearch | None = None,
    web_fetch: SafeWebFetcher | None = None,
) -> ToolRegistry:
    """Build the standard registry with the three workspace tools."""

    return ToolRegistry(build_default_tools(workspace, web_search=web_search, web_fetch=web_fetch))
