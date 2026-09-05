"""Local stdio MCP server that exposes tools across two pages."""

from __future__ import annotations

import asyncio

from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server


def _tool(name: str) -> types.Tool:
    return types.Tool(
        name=name,
        description=f"Tool from {name}",
        input_schema={"type": "object", "additionalProperties": False},
    )


async def list_tools(ctx, params) -> types.ListToolsResult:
    cursor = params.cursor if params is not None else None
    if cursor is None:
        return types.ListToolsResult(tools=[_tool("first_page")], next_cursor="second-page")
    if cursor == "second-page":
        return types.ListToolsResult(tools=[_tool("second_page")])
    return types.ListToolsResult(tools=[])


async def call_tool(ctx, params) -> types.CallToolResult:
    return types.CallToolResult(content=[types.TextContent(type="text", text=f"called {params.name}")])


server = Server("paginated-tools-test", on_list_tools=list_tools, on_call_tool=call_tool)


async def main() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
