"""Local tools and their registry."""

from .base import ConfirmationRequired, Tool, ToolError, ToolExecutor, ToolInvocationContext
from .catalog import build_tool_registry
from .command import WorkspaceCommand
from .delegation import delegation_tools
from .filesystem import WorkspaceFiles
from .registry import ToolRegistry
from .web import DdgrWebSearch, SafeWebFetcher

__all__ = [
    "ConfirmationRequired",
    "DdgrWebSearch",
    "SafeWebFetcher",
    "Tool",
    "ToolError",
    "ToolExecutor",
    "ToolInvocationContext",
    "ToolRegistry",
    "WorkspaceCommand",
    "WorkspaceFiles",
    "build_tool_registry",
    "delegation_tools",
]
