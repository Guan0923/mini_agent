"""Local tools and their registry."""

from .base import ConfirmationRequired, Tool, ToolError, ToolExecutor
from .command import WorkspaceCommand
from .defaults import build_default_tools
from .factory import build_tool_registry
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
    "build_default_tools",
    "build_tool_registry",
]
