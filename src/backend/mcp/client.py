"""Long-lived external stdio MCP clients and layered configuration."""

from __future__ import annotations

import asyncio
import atexit
import os
import threading
import tomllib
from concurrent.futures import TimeoutError as FutureTimeoutError
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from backend.tools import Tool, ToolError


@dataclass(frozen=True)
class McpServerConfig:
    name: str
    command: str
    args: tuple[str, ...] = ()
    cwd: str | None = None
    env: dict[str, str] | None = None


_MANAGERS: list[ExternalMcpManager] = []
_SHUTDOWN_TIMEOUT = 5.0


class ExternalMcpManager:
    def __init__(self, configs: tuple[McpServerConfig, ...]) -> None:
        self._configs = configs
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, name="mini-agent-mcp", daemon=True)
        self._thread.start()
        self._stack: AsyncExitStack | None = None
        self._sessions: dict[str, ClientSession] = {}
        try:
            self.definitions = self._submit(self._start())
        except Exception:
            self.close()
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
            result = self._submit(self._sessions[server_name].call_tool(tool_name, arguments))
        except Exception as exc:
            raise ToolError(f"MCP tool {server_name}/{tool_name} failed: {type(exc).__name__}") from exc
        if result.isError:
            raise ToolError("; ".join(_content_text(result.content)) or "MCP server returned an error.")
        return "\n".join(_content_text(result.content)) or "MCP tool completed without text output."

    def close(self) -> None:
        if not self._loop.is_running():
            return
        if self._stack is not None:
            try:
                self._submit(self._stack.aclose(), timeout=_SHUTDOWN_TIMEOUT)
            except Exception:
                pass
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=_SHUTDOWN_TIMEOUT)


def load_server_configs(global_file: Path, project_file: Path) -> tuple[McpServerConfig, ...]:
    servers = {item.name: item for item in _read_servers(global_file)}
    servers.update({item.name: item for item in _read_servers(project_file)})
    return tuple(servers[name] for name in sorted(servers))


def load_external_tools(global_file: Path, project_file: Path) -> tuple[Tool, ...]:
    configs = load_server_configs(global_file, project_file)
    if not configs:
        return ()
    try:
        manager = ExternalMcpManager(configs)
    except Exception as exc:
        raise ToolError(f"Cannot initialize external MCP servers: {type(exc).__name__}") from exc
    _MANAGERS.append(manager)
    tools: list[Tool] = []
    for server in configs:
        for definition in manager.definitions[server.name]:
            definition_name = str(getattr(definition, "name"))
            schema_value = getattr(definition, "inputSchema", {})
            schema = dict(schema_value) if isinstance(schema_value, dict) else {"type": "object"}
            tools.append(
                Tool(
                    f"mcp_{server.name}_{definition_name}",
                    f"MCP {server.name}: {getattr(definition, 'description', None) or definition_name}",
                    _handler(manager, server.name, definition_name),
                    schema,
                    requires_confirmation=True,
                    read_only=False,
                )
            )
    return tuple(tools)


def close_external_tools() -> None:
    while _MANAGERS:
        _MANAGERS.pop().close()


def _handler(manager: ExternalMcpManager, server_name: str, tool_name: str):
    def invoke(**arguments: Any) -> str:
        return manager.call(server_name, tool_name, arguments)

    return invoke


def _parameters(server: McpServerConfig) -> StdioServerParameters:
    environment = {**os.environ, **(server.env or {})}
    return StdioServerParameters(
        command=server.command,
        args=list(server.args),
        cwd=server.cwd,
        env=environment,
    )


def _content_text(content: list[object]) -> list[str]:
    return [str(getattr(item, "text")) for item in content if isinstance(getattr(item, "text", None), str)]


def _read_servers(path: Path) -> tuple[McpServerConfig, ...]:
    if not path.exists():
        return ()
    try:
        with path.open("rb") as handle:
            values = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ToolError(f"Invalid MCP configuration {path}: {exc}") from exc
    servers = values.get("servers", {})
    if not isinstance(servers, dict):
        raise ToolError(f"{path}: [servers] must be a table.")
    result: list[McpServerConfig] = []
    for name, value in servers.items():
        if not isinstance(name, str) or not isinstance(value, dict):
            raise ToolError(f"{path}: server entries must be named tables.")
        command, args, cwd, env = value.get("command"), value.get("args", []), value.get("cwd"), value.get("env")
        if (
            not isinstance(command, str)
            or not command.strip()
            or not isinstance(args, list)
            or not all(isinstance(item, str) for item in args)
        ):
            raise ToolError(f"{path}: servers.{name} requires command and string args.")
        if (
            cwd is not None
            and not isinstance(cwd, str)
            or env is not None
            and (
                not isinstance(env, dict)
                or not all(isinstance(key, str) and isinstance(item, str) for key, item in env.items())
            )
        ):
            raise ToolError(f"{path}: servers.{name} has invalid cwd or env.")
        result.append(McpServerConfig(name, command, tuple(args), cwd, dict(env) if isinstance(env, dict) else None))
    return tuple(result)


atexit.register(close_external_tools)
