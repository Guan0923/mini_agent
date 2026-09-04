"""Local stdio MCP server that exposes tools across two pages."""

from __future__ import annotations

import asyncio
from typing import Any

from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

server = Server("paginated-tools-test")


def _tool(name: str) -> types.Tool:
    return types.Tool(
        name=name,
        description=f"Tool from {name}",
        inputSchema={"type": "object", "additionalProperties": False},
    )


@server.list_tools()
async def list_tools(request: types.ListToolsRequest) -> types.ListToolsResult:
    cursor = request.params.cursor if request.params is not None else None
    if cursor is None:
        return types.ListToolsResult(tools=[_tool("first_page")], nextCursor="second-page")
    if cursor == "second-page":
        return types.ListToolsResult(tools=[_tool("second_page")])
    return types.ListToolsResult(tools=[])


@server.call_tool(validate_input=False)
async def call_tool(name: str, arguments: dict[str, Any]) -> types.CallToolResult:
    del arguments
    return types.CallToolResult(content=[types.TextContent(type="text", text=f"called {name}")])


async def main() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
