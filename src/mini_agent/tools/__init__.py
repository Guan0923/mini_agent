"""Local tools and their registry."""

from .base import ConfirmationRequired, Tool, ToolError, ToolExecutor
from .catalog import build_tool_registry
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
    "build_tool_registry",
]
