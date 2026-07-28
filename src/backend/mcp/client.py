"""Long-lived, application-owned external stdio MCP clients."""

from __future__ import annotations

import asyncio
import json
import os
import threading
from collections.abc import Iterator, Sequence
from concurrent.futures import TimeoutError as FutureTimeoutError
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any, overload

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from backend.tools import Tool, ToolError

from .config import McpServerConfig, McpSettings, read_server_configs, valid_tool_name

_MAX_RESULT_CHARS = 20_000


class ExternalMcpManager:
    """Own one event loop and the stdio sessions started on it."""

    def __init__(self, configs: tuple[McpServerConfig, ...], settings: McpSettings | None = None) -> None:
        self._configs = configs
        self._settings = settings or McpSettings()
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, name="mini-agent-mcp", daemon=True)
        self._stack: AsyncExitStack | None = None
        self._sessions: dict[str, ClientSession] = {}
        self._closed = False
        self._thread.start()
        try:
            self.definitions = self._submit(self._start(), timeout=self._settings.initialization_timeout_seconds)
        except Exception:
            try:
                self.close()
            except ToolError:
                pass
            raise

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def _submit(self, coroutine, *, timeout: float | None = None):
        future = asyncio.run_coroutine_threadsafe(coroutine, self._loop)
        try:
            return future.result(timeout=timeout)
        except FutureTimeoutError:
            future.cancel()
            raise

    async def _start(self) -> dict[str, list[object]]:
        self._stack = AsyncExitStack()
        await self._stack.__aenter__()
        definitions: dict[str, list[object]] = {}
        for server in self._configs:
            read, write = await self._stack.enter_async_context(stdio_client(_parameters(server)))
            session = await self._stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            self._sessions[server.name] = session
            definitions[server.name] = list((await session.list_tools()).tools)
        return definitions

    def call(self, server_name: str, tool_name: str, arguments: dict[str, Any]) -> str:
        try:
            result = self._submit(
                self._sessions[server_name].call_tool(tool_name, arguments),
                timeout=self._settings.call_timeout_seconds,
            )
        except FutureTimeoutError as exc:
            raise ToolError(f"MCP tool {server_name}/{tool_name} timed out.") from exc
        except Exception as exc:
            raise ToolError(f"MCP tool {server_name}/{tool_name} failed: {type(exc).__name__}") from exc
        rendered = _render_result(result)
        if getattr(result, "isError", False):
            raise ToolError(rendered or "MCP server returned an error.")
        return rendered or "MCP tool completed without output."

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        timed_out = False
        if self._loop.is_running() and self._stack is not None:
            try:
                self._submit(self._stack.aclose(), timeout=self._settings.shutdown_timeout_seconds)
            except FutureTimeoutError:
                timed_out = True
            except Exception:
                pass
        if self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=self._settings.shutdown_timeout_seconds)
        if self._thread.is_alive():
            timed_out = True
        else:
            self._loop.close()
        if timed_out:
            raise ToolError("MCP shutdown timed out.")


class ExternalMcpResources(Sequence[Tool]):
    """The discovered tools and the exact manager that owns them."""

    def __init__(self, tools: tuple[Tool, ...] = (), manager: object | None = None) -> None:
        self.tools = tools
        self.manager = manager
        self._closed = False

    def __iter__(self) -> Iterator[Tool]:
        return iter(self.tools)

    def __len__(self) -> int:
        return len(self.tools)

    @overload
    def __getitem__(self, index: int) -> Tool: ...

    @overload
    def __getitem__(self, index: slice) -> tuple[Tool, ...]: ...

    def __getitem__(self, index: int | slice) -> Tool | tuple[Tool, ...]:
        return self.tools[index]

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        close = getattr(self.manager, "close", None)
        if callable(close):
            close()


def load_server_configs(global_file: Path, project_file: Path) -> tuple[McpServerConfig, ...]:
    """Compatibility helper that parses and merges two configuration files."""

    servers = {item.name: item for item in read_server_configs(global_file)}
    servers.update({item.name: item for item in read_server_configs(project_file)})
    return tuple(servers[name] for name in sorted(servers))


def load_external_tools(
    global_file: Path,
    project_file: Path,
    settings: McpSettings | None = None,
) -> ExternalMcpResources:
    return start_external_tools(load_server_configs(global_file, project_file), settings)


def start_external_tools(
    configs: tuple[McpServerConfig, ...],
    settings: McpSettings | None = None,
) -> ExternalMcpResources:
    if not configs:
        return ExternalMcpResources()
    try:
        manager = ExternalMcpManager(configs) if settings is None else ExternalMcpManager(configs, settings)
    except FutureTimeoutError as exc:
        raise ToolError("Cannot initialize external MCP servers: initialization timed out.") from exc
    except Exception as exc:
        raise ToolError(f"Cannot initialize external MCP servers: {type(exc).__name__}") from exc

    tools: list[Tool] = []
    names: set[str] = set()
    try:
        for server in configs:
            for definition in manager.definitions[server.name]:
                definition_name = str(getattr(definition, "name", ""))
                external_name = f"mcp_{server.name}_{definition_name}"
                if not valid_tool_name(external_name):
                    raise ToolError(
                        f"MCP tool name {external_name!r} must use letters, digits, '_' or '-' "
                        "and be at most 64 characters."
                    )
                if external_name in names:
                    raise ToolError(f"Duplicate external MCP tool name: {external_name}")
                names.add(external_name)
                schema_value = getattr(definition, "inputSchema", {})
                schema = dict(schema_value) if isinstance(schema_value, dict) else {"type": "object"}
                tools.append(
                    Tool(
                        external_name,
                        f"MCP {server.name}: {getattr(definition, 'description', None) or definition_name}",
                        _handler(manager, server.name, definition_name),
                        schema,
                        requires_confirmation=True,
                        read_only=False,
                    )
                )
    except Exception:
        try:
            manager.close()
        except ToolError:
            pass
        raise
    return ExternalMcpResources(tuple(tools), manager)


def close_external_tools() -> None:
    """Deprecated no-op: resources are now closed by their owning runner."""


def _handler(manager: ExternalMcpManager, server_name: str, tool_name: str):
    def invoke(**arguments: Any) -> str:
        return manager.call(server_name, tool_name, arguments)

    return invoke


def _parameters(server: McpServerConfig) -> StdioServerParameters:
    return StdioServerParameters(
        command=server.command,
        args=list(server.args),
        cwd=server.cwd,
        env={**os.environ, **(server.env or {})},
    )


def _render_result(result: object) -> str:
    rendered: list[object] = []
    for item in list(getattr(result, "content", ())):
        text = getattr(item, "text", None)
        if isinstance(text, str):
            rendered.append(text)
            continue
        resource = getattr(item, "resource", None)
        resource_text = getattr(resource, "text", None)
        if isinstance(resource_text, str):
            rendered.append(
                {
                    "type": "resource",
                    "uri": str(getattr(resource, "uri", "")),
                    "mimeType": getattr(resource, "mimeType", None),
                    "text": resource_text,
                }
            )
            continue
        data = getattr(item, "data", None)
        blob = getattr(resource, "blob", None)
        rendered.append(
            {
                "type": str(getattr(item, "type", type(item).__name__)),
                "mimeType": getattr(item, "mimeType", getattr(resource, "mimeType", None)),
                "size": len(data) if isinstance(data, str) else len(blob) if isinstance(blob, str) else None,
                "content_omitted": True,
            }
        )
    structured = getattr(result, "structuredContent", None)
    if structured is not None:
        rendered.append({"type": "structured", "value": structured})
    if not rendered:
        return ""
    value = (
        "\n".join(str(item) for item in rendered)
        if all(isinstance(item, str) for item in rendered)
        else json.dumps(rendered, ensure_ascii=False, default=str)
    )
    if len(value) <= _MAX_RESULT_CHARS:
        return value
    omitted = len(value) - _MAX_RESULT_CHARS
    return f"{value[:_MAX_RESULT_CHARS]}… ({omitted} characters omitted)"
