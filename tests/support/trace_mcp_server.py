"""Small stdio MCP server used only by the Playwright Trace audit flow."""

from mcp.server import MCPServer

server = MCPServer("trace-audit-e2e")


@server.tool()
def inspect_trace(label: str = "trace") -> str:
    """Return one deterministic local MCP result."""

    return f"MCP inspected {label}."


if __name__ == "__main__":
    server.run(transport="stdio")
