"""Expose Mini-Agent's non-interactive read-only tools through MCP."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from mcp import types
from mcp.server import Server

from backend.domain import ToolSpec, safe_error_message
from backend.tools import ToolError, ToolExecutor, build_tool_registry


class McpToolAdapter:
    """Translate the existing tool contract into safe MCP tool calls."""

    def __init__(self, tools: ToolExecutor) -> None:
        self._tools = tools

    @property
    def specs(self) -> list[ToolSpec]:
        """Return tools that can run without an interactive approval callback."""

        return [spec for spec in self._tools.read_only_specs() if not self._tools.requires_confirmation(spec.name)]

    def definitions(self) -> list[types.Tool]:
        """Return MCP tool definitions derived from the registry's JSON Schemas."""

        return [
            types.Tool(name=spec.name, description=spec.description, inputSchema=spec.parameters) for spec in self.specs
        ]

    def invoke(self, name: str, arguments: dict[str, Any] | None) -> types.CallToolResult:
        """Call one exposed tool and convert expected failures into MCP results."""

        if name not in {spec.name for spec in self.specs}:
            return self._error(f"Tool is not available through this MCP server: {name}")
        try:
            text = self._tools.invoke(name, arguments or {})
        except ToolError as exc:
            return self._error(safe_error_message(exc))
        return types.CallToolResult(content=[types.TextContent(type="text", text=text)])

    @staticmethod
    def _error(message: str) -> types.CallToolResult:
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=message)],
            isError=True,
        )


def create_server(workspace: Path) -> Server:
    """Create a stdio-ready MCP server for one confined workspace."""

    adapter = McpToolAdapter(build_tool_registry(workspace))
    server = Server("mini-agent")

    @server.list_tools()
    async def list_tools() -> Sequence[types.Tool]:
        return adapter.definitions()

    @server.call_tool(validate_input=False)
    async def call_tool(name: str, arguments: dict[str, Any]) -> types.CallToolResult:
        return adapter.invoke(name, arguments)

    return server
