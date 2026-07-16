"""Local tools and their registry."""

from .base import ConfirmationRequired, Tool, ToolError, ToolExecutor
from .calculator import calculate
from .catalog import build_workspace_tool_registry, build_workspace_tools
from .command import WorkspaceCommand
from .registry import ToolRegistry
from .web import DdgrWebSearch, SafeWebFetcher

__all__ = [
    "ConfirmationRequired",
    "DdgrWebSearch",
    "SafeWebFetcher",
    "Tool",
    "ToolError",
    "ToolExecutor",
    "ToolRegistry",
    "WorkspaceCommand",
    "build_workspace_tool_registry",
    "build_workspace_tools",
    "calculate",
]
