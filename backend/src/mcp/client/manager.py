"""Run-local event-loop ownership for external MCP connections."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Iterator, Sequence
from concurrent.futures import TimeoutError as FutureTimeoutError
from typing import Any, overload

from mcp import Client, types

from backend.domain import safe_error_message
from backend.jobs import AdmissionPolicy, JobLane, JobRegistry, JobScope, JobScopeKind, ServiceJob
from backend.tools import Tool, ToolError

from ..config import McpServerConfig, McpSettings
from .adapters import _render_result, render_mcp_value
from .subscriptions import ResourceSubscriptions
from .transports import connection_transport


class _McpServiceDriver:
    """Job-facing health/stop view over one manager-owned MCP session."""

    def __init__(self, manager: ExternalMcpManager, server_name: str) -> None:
        self.manager = manager
        self.server_name = server_name

    def start(self) -> object:
        return self.manager._ensure_server(self.server_name)

    def check(self, _handle: object) -> bool:
        return not self.manager._closed and self.manager.server_health.get(self.server_name) == "healthy"

    def stop(self, _handle: object) -> None:
        self.manager._stop_server(self.server_name)


async def _list_all_tools(session: Client) -> list[types.Tool]:
    """Collect every tools/list page while tolerating cursor cycles."""

    definitions: list[types.Tool] = []
    seen_cursors: set[str] = set()
    page = await session.list_tools()
    while True:
        definitions.extend(page.tools)
        cursor = page.next_cursor
        if cursor is None or cursor in seen_cursors:
            return definitions
        seen_cursors.add(cursor)
        page = await session.list_tools(cursor=cursor)


class ExternalMcpManager:
    """Own one event loop and its stdio/HTTP clients and subscriptions."""

    def __init__(
        self,
        configs: tuple[McpServerConfig, ...],
        settings: McpSettings | None = None,
        *,
        job_registry: JobRegistry | None = None,
        job_scope: JobScope | None = None,
    ) -> None:
        self._configs = configs
        self._settings = settings or McpSettings()
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, name="mini-agent-mcp", daemon=True)
        self._connections: dict[str, asyncio.Task] = {}
        self._stops: dict[str, asyncio.Event] = {}
        self._sessions: dict[str, Client] = {}
        self.capabilities: dict[str, types.ServerCapabilities] = {}
        self.protocol_versions: dict[str, str] = {}
        self._subscriptions: dict[str, ResourceSubscriptions] = {}
        self.failed_servers: dict[str, str] = {}
        self.server_health: dict[str, str] = {}
        self.service_jobs: dict[str, ServiceJob] = {}
        self._job_registry = job_registry
        self._job_scope = job_scope
        self._closed = False
        self._thread.start()
        try:
            self.definitions = self._submit(self._start(), timeout=self._settings.initialization_timeout_seconds)
            self._register_service_jobs()
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
        definitions: dict[str, list[object]] = {}
        for server in self._configs:
            try:
                definitions[server.name] = await self._open_server(server)
                self.server_health[server.name] = "healthy"
            except Exception as exc:
                # One server must not prevent independent servers from
                # starting.  Keep only the exception class in diagnostics.
                self.failed_servers[server.name] = type(exc).__name__
                self.server_health[server.name] = "failed"
        return definitions

    async def _open_server(self, server: McpServerConfig) -> list[object]:
        ready = self._loop.create_future()
        self._stops[server.name] = asyncio.Event()
        task = asyncio.create_task(self._serve(server, ready))
        self._connections[server.name] = task
        try:
            return await ready
        except BaseException:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            self._connections.pop(server.name, None)
            self._stops.pop(server.name, None)
            raise

    async def _serve(self, server: McpServerConfig, ready: asyncio.Future) -> None:
        subscriptions = self._subscriptions.setdefault(server.name, ResourceSubscriptions())
        try:
            # Client/transport task groups must enter and leave in the same task.
            async with Client(
                connection_transport(server, self._settings),
                mode="auto",
                cache=None,
                message_handler=subscriptions.notification,
                read_timeout_seconds=self._settings.call_timeout_seconds,
            ) as client:
                self._sessions[server.name] = client
                self.capabilities[server.name] = client.server_capabilities
                self.protocol_versions[server.name] = client.protocol_version
                definitions = await _list_all_tools(client) if client.server_capabilities.tools is not None else []
                ready.set_result(definitions)
                try:
                    await self._stops[server.name].wait()
                finally:
                    await subscriptions.close(preserve_updates=not self._closed)
        except asyncio.CancelledError:
            if not ready.done():
                ready.cancel()
            raise
        except Exception as exc:
            if not ready.done():
                ready.set_exception(ToolError(f"MCP connection failed ({type(exc).__name__})."))
            self.server_health[server.name] = "down"
        finally:
            self._sessions.pop(server.name, None)
            subscriptions.lost()

    async def _close_server(self, server_name: str) -> None:
        stop = self._stops.pop(server_name, None)
        if stop is not None:
            stop.set()
        task = self._connections.pop(server_name, None)
        if task is not None:
            await task
        self.server_health[server_name] = "down"

    def _server_config(self, server_name: str) -> McpServerConfig:
        for config in self._configs:
            if config.name == server_name:
                return config
        raise ToolError(f"MCP server {server_name} is unavailable.")

    def _ensure_server(self, server_name: str) -> object:
        if self._closed:
            raise ToolError("MCP manager is closed.")
        if server_name in self._sessions:
            self.server_health[server_name] = "healthy"
            return (server_name, id(self._sessions[server_name]))
        try:
            definitions = self._submit(
                self._open_server(self._server_config(server_name)),
                timeout=self._settings.initialization_timeout_seconds,
            )
            self.definitions[server_name] = definitions
            self.failed_servers.pop(server_name, None)
            return (server_name, id(self._sessions[server_name]))
        except FutureTimeoutError as exc:
            raise ToolError(safe_error_message(exc)) from exc
        except Exception as exc:
            self.failed_servers[server_name] = type(exc).__name__
            self.server_health[server_name] = "failed"
            raise ToolError(safe_error_message(exc)) from exc

    def _stop_server(self, server_name: str) -> None:
        if self._closed:
            return
        try:
            self._submit(self._close_server(server_name), timeout=self._settings.shutdown_timeout_seconds)
        except FutureTimeoutError as exc:
            raise ToolError(safe_error_message(exc)) from exc

    def call(self, server_name: str, tool_name: str, arguments: dict[str, Any]) -> str:
        if server_name not in self._sessions:
            raise ToolError(f"MCP server {server_name} is unavailable.")
        try:
            result = self._submit(
                self._sessions[server_name].call_tool(tool_name, arguments),
                timeout=self._settings.call_timeout_seconds,
            )
        except FutureTimeoutError as exc:
            getattr(self, "server_health", {})[server_name] = "degraded"
            job = getattr(self, "service_jobs", {}).get(server_name)
            if job is not None:
                job.report_failure()
            raise ToolError(safe_error_message(exc)) from exc
        except Exception as exc:
            getattr(self, "server_health", {})[server_name] = "degraded"
            job = getattr(self, "service_jobs", {}).get(server_name)
            if job is not None:
                job.report_failure()
            raise ToolError(safe_error_message(exc)) from exc
        rendered = _render_result(result)
        if getattr(result, "is_error", False):
            job = getattr(self, "service_jobs", {}).get(server_name)
            if job is not None:
                job.report_failure()
            raise ToolError(rendered or "MCP server returned an error.")
        job = getattr(self, "service_jobs", {}).get(server_name)
        if job is not None:
            job.report_success()
        return rendered or "MCP tool completed without output."

    def request(self, server: str, method: str, **arguments: Any) -> str:
        try:
            return self._submit(self._request(server, method, arguments), timeout=self._settings.call_timeout_seconds)
        except Exception as exc:
            if isinstance(exc, ToolError):
                raise
            raise ToolError(f"MCP {method} failed ({type(exc).__name__}).") from None

    async def _request(self, server: str, method: str, arguments: dict[str, Any]) -> str:
        client = self._sessions.get(server)
        if client is None:
            raise ToolError("MCP server is unavailable.")
        subscriptions = self._subscriptions[server]
        if method == "get_resource_updates":
            if client.protocol_version != "2026-07-28" and any(
                status == "active" for status in subscriptions.states.values()
            ):
                try:
                    await client.session.send_ping()
                except Exception:
                    subscriptions.lost()
            return render_mcp_value(subscriptions.snapshot())
        if method == "subscribe_resource":
            result = await subscriptions.subscribe(client, arguments["uri"])
        elif method == "unsubscribe_resource":
            result = await subscriptions.unsubscribe(client, arguments["uri"])
        else:
            revision = subscriptions.revision
            result = await getattr(client, method)(**arguments)
            if method == "read_resource" and subscriptions.changed.get(arguments["uri"], 0) <= revision:
                subscriptions.changed.pop(arguments["uri"], None)
        return render_mcp_value(result)

    def describe(self, server: str) -> dict[str, Any]:
        return self._submit(self._describe(server), timeout=self._settings.initialization_timeout_seconds)

    async def _describe(self, server: str) -> dict[str, Any]:
        client = self._sessions[server]
        caps = self.capabilities[server]
        counts = {"tools": len(self.definitions[server]), "resources": 0, "resource_templates": 0, "prompts": 0}
        for capability, method, field in (
            ("resources", "list_resources", "resources"),
            ("resources", "list_resource_templates", "resource_templates"),
            ("prompts", "list_prompts", "prompts"),
        ):
            if getattr(caps, capability) is None:
                continue
            cursor = None
            seen: set[str] = set()
            while True:
                page = await getattr(client, method)(cursor=cursor)
                counts[field] += len(getattr(page, field))
                cursor = page.next_cursor
                if cursor is None or cursor in seen:
                    break
                seen.add(cursor)
        return {
            "protocol_version": client.protocol_version,
            "capabilities": [name for name in ("tools", "resources", "prompts") if getattr(caps, name) is not None],
            "counts": counts,
        }

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        timed_out = False
        if self._loop.is_running():
            try:
                self._submit(self._close_all_servers(), timeout=self._settings.shutdown_timeout_seconds)
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

    async def _close_all_servers(self) -> None:
        for server_name in tuple(self._connections):
            await self._close_server(server_name)

    def _register_service_jobs(self) -> None:
        if self._job_registry is None:
            return
        scope = self._job_scope or self._job_registry.root_scope().child(JobScopeKind.RUNNER)
        for server_name in self._sessions:
            job = ServiceJob(
                self._job_registry.new_job_id(),
                _McpServiceDriver(self, server_name),
                init_timeout_seconds=self._settings.initialization_timeout_seconds,
                max_failures=self._settings.health_failure_threshold,
                max_restarts=self._settings.rebuild_failure_threshold,
            )
            self._job_registry.submit(
                job,
                scope=scope,
                lane=JobLane.SERVICE,
                admission=AdmissionPolicy(queue_timeout_seconds=30.0),
            )
            self.service_jobs[server_name] = job


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
