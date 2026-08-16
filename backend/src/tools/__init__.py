"""Local tools and their registry."""

from .base import ConfirmationRequired, Tool, ToolError, ToolExecutor, ToolInvocationContext
from .catalog import build_tool_registry
from .command import WorkspaceCommand
from .default_tools.todo import todo_tools
from .delegation import delegation_tools
from .filesystem import WorkspaceFiles
from .registry import ToolRegistry
from .web import DdgrWebSearch, DuckDuckGoWebSearch, SafeWebFetcher

__all__ = [
    "ConfirmationRequired",
    "DdgrWebSearch",
    "DuckDuckGoWebSearch",
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
    "todo_tools",
]
