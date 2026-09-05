"""MCP transports with application-owned processes and HTTP credentials."""

from __future__ import annotations

from contextlib import asynccontextmanager

import httpx2
from mcp.client.streamable_http import streamable_http_client

from backend.tools import ToolError

from ..config import McpServerConfig, McpSettings
from ..controlled_stdio import controlled_stdio_client
from .adapters import _parameters, _resolve_environment_reference


@asynccontextmanager
async def connection_transport(server: McpServerConfig, settings: McpSettings):
    if server.transport == "stdio":
        async with controlled_stdio_client(_parameters(server)) as streams:
            yield streams
        return
    headers = dict(server.headers or {})
    for name, reference in (server.header_refs or {}).items():
        value = _resolve_environment_reference(reference, name)
        if "\r" in value or "\n" in value or len(value) > 4096:
            raise ToolError("Invalid MCP credential header value.")
        headers[name] = value
    async with httpx2.AsyncClient(
        headers=headers,
        follow_redirects=False,
        verify=True,
        timeout=httpx2.Timeout(settings.call_timeout_seconds, connect=settings.initialization_timeout_seconds),
    ) as http:
        async with streamable_http_client(server.url, http_client=http) as streams:
            yield streams
