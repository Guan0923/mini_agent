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

from backend.jobs import AdmissionPolicy, JobLane, JobRegistry, JobScope, JobScopeKind, ServiceJob
from backend.tools import Tool, ToolError

from .config import McpServerConfig, McpSettings, read_server_configs, valid_tool_name
from .controlled_stdio import StdioServerParameters, controlled_stdio_client

_MAX_RESULT_CHARS = 20_000


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


class ExternalMcpManager:
    """Own one event loop and the stdio sessions started on it."""

    def __init__(
        self,
        configs: tuple[McpServerConfig, ...],
        settings: McpSettings | None = None,
        *,
        job_registry: JobRegistry | None = None,
        job_scope: JobScope | None = None,
        sandbox_launcher=None,
        sandbox_policy_factory=None,
    ) -> None:
        self._configs = configs
        self._settings = settings or McpSettings()
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, name="mini-agent-mcp", daemon=True)
        self._stack: AsyncExitStack | None = None
        self._server_stacks: dict[str, AsyncExitStack] = {}
        self._sessions: dict[str, ClientSession] = {}
        self.failed_servers: dict[str, str] = {}
        self.server_health: dict[str, str] = {}
        self.service_jobs: dict[str, ServiceJob] = {}
        self._job_registry = job_registry
        self._job_scope = job_scope
        self._sandbox_launcher = sandbox_launcher
        self._sandbox_policy_factory = sandbox_policy_factory
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
        """Open one server in an independent exit stack and return tools."""
        stack = AsyncExitStack()
        await stack.__aenter__()
        try:
            policy = self._sandbox_policy_factory(server) if self._sandbox_policy_factory is not None else None
            read, write = await stack.enter_async_context(
                controlled_stdio_client(
                    _parameters(server),
                    sandbox_launcher=self._sandbox_launcher,
                    sandbox_policy=policy,
                )
            )
            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            definitions = list((await session.list_tools()).tools)
        except BaseException:
            await stack.aclose()
            raise
        self._server_stacks[server.name] = stack
        self._sessions[server.name] = session
        self.server_health[server.name] = "healthy"
        return definitions

    async def _close_server(self, server_name: str) -> None:
        stack = self._server_stacks.pop(server_name, None)
        self._sessions.pop(server_name, None)
        self.server_health[server_name] = "down"
        if stack is not None:
            await stack.aclose()

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
            raise ToolError("MCP server initialization timed out.") from exc
        except Exception as exc:
            self.failed_servers[server_name] = type(exc).__name__
            self.server_health[server_name] = "failed"
            raise ToolError(f"MCP server rebuild failed: {type(exc).__name__}") from exc

    def _stop_server(self, server_name: str) -> None:
        if self._closed:
            return
        try:
            self._submit(self._close_server(server_name), timeout=self._settings.shutdown_timeout_seconds)
        except FutureTimeoutError as exc:
            raise ToolError("MCP server shutdown timed out.") from exc

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
            raise ToolError(f"MCP tool {server_name}/{tool_name} timed out.") from exc
        except Exception as exc:
            getattr(self, "server_health", {})[server_name] = "degraded"
            job = getattr(self, "service_jobs", {}).get(server_name)
            if job is not None:
                job.report_failure()
            raise ToolError(f"MCP tool {server_name}/{tool_name} failed: {type(exc).__name__}") from exc
        rendered = _render_result(result)
        if getattr(result, "isError", False):
            job = getattr(self, "service_jobs", {}).get(server_name)
            if job is not None:
                job.report_failure()
            raise ToolError(rendered or "MCP server returned an error.")
        job = getattr(self, "service_jobs", {}).get(server_name)
        if job is not None:
            job.report_success()
        return rendered or "MCP tool completed without output."

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
        for server_name in tuple(self._server_stacks):
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
            if self._sandbox_policy_factory is not None:
                policy = self._sandbox_policy_factory(self._server_config(server_name))
                job.sandbox_policy = policy
                setter = getattr(job, "_set_sandbox_info", None)
                if callable(setter):
                    raw = policy.to_dict()
                    setter(
                        {
                            "enforced": bool(raw.get("enforced", True)),
                            "file_mode": raw.get("file_mode", "read_only"),
                            "network_mode": raw.get("network_mode", "no_network"),
                            "limits": raw.get("limits", {}),
                            "failure_code": None,
                            "cleanup_pending": False,
                        }
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


def load_server_configs(global_file: Path, project_file: Path | None = None) -> tuple[McpServerConfig, ...]:
    """Compatibility helper that parses the single user-level configuration file."""

    del project_file
    return read_server_configs(global_file, reject_plaintext_secrets=True)


def load_external_tools(
    global_file: Path,
    project_file: Path | None = None,
    settings: McpSettings | None = None,
    job_registry: JobRegistry | None = None,
    job_scope: JobScope | None = None,
    sandbox_launcher=None,
    sandbox_policy_factory=None,
) -> ExternalMcpResources:
    del project_file
    return start_external_tools(
        read_server_configs(global_file, reject_plaintext_secrets=True),
        settings,
        job_registry=job_registry,
        job_scope=job_scope,
        sandbox_launcher=sandbox_launcher,
        sandbox_policy_factory=sandbox_policy_factory,
    )


def start_external_tools(
    configs: tuple[McpServerConfig, ...],
    settings: McpSettings | None = None,
    *,
    job_registry: JobRegistry | None = None,
    job_scope: JobScope | None = None,
    sandbox_launcher=None,
    sandbox_policy_factory=None,
) -> ExternalMcpResources:
    if not configs:
        return ExternalMcpResources()
    try:
        manager_kwargs = {
            "job_registry": job_registry,
            "job_scope": job_scope,
            "sandbox_launcher": sandbox_launcher,
            "sandbox_policy_factory": sandbox_policy_factory,
        }
        manager_kwargs = {name: value for name, value in manager_kwargs.items() if value is not None}
        manager = (
            ExternalMcpManager(configs, **manager_kwargs)
            if settings is None
            else ExternalMcpManager(configs, settings, **manager_kwargs)
        )
    except FutureTimeoutError as exc:
        raise ToolError("Cannot initialize external MCP servers: initialization timed out.") from exc
    except Exception as exc:
        raise ToolError(f"Cannot initialize external MCP servers: {type(exc).__name__}") from exc

    tools: list[Tool] = []
    names: set[str] = set()
    try:
        for server in configs:
            for definition in manager.definitions.get(server.name, []):
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
    if not manager.definitions and configs:
        try:
            manager.close()
        except ToolError:
            pass
        raise ToolError("Cannot initialize external MCP servers: all configured servers failed.")
    return ExternalMcpResources(tuple(tools), manager)


def close_external_tools() -> None:
    """Deprecated no-op: resources are now closed by their owning runner."""


def _handler(manager: ExternalMcpManager, server_name: str, tool_name: str):
    def invoke(**arguments: Any) -> str:
        return manager.call(server_name, tool_name, arguments)

    return invoke


def _parameters(server: McpServerConfig) -> StdioServerParameters:
    allowed = {"PATH"}
    if os.name == "nt":
        allowed.update({"SystemRoot", "ComSpec", "PATHEXT", "USERPROFILE"})
    else:
        allowed.add("HOME")
    environment = {name: value for name in allowed if (value := os.environ.get(name)) is not None}
    environment.update(server.env or {})
    for name, reference in (server.env_refs or {}).items():
        environment[name] = _resolve_environment_reference(reference, name)
    return StdioServerParameters(
        command=server.command,
        args=list(server.args),
        cwd=server.cwd,
        env=environment,
    )


def _resolve_environment_reference(reference: str, name: str) -> str:
    """Resolve a configured MCP secret only at process start.

    The reference itself is safe to persist in ``servers.toml``.  Values are
    loaded from the process environment or the OS credential vault and are
    never included in configuration digests, review text, or exception
    messages.
    """

    if reference.startswith("env://"):
        environment_name = reference[6:]
        value = os.environ.get(environment_name)
        if value is None:
            raise ToolError(f"MCP environment reference for {name} is unavailable.")
        return value
    if reference.startswith("keyring://"):
        try:
            import keyring

            _, location = reference.split("://", 1)
            service, account = location.split("/", 1)
            value = keyring.get_password(service, account)
        except (ImportError, ValueError, OSError) as exc:
            raise ToolError(f"MCP credential reference for {name} is unavailable.") from exc
        if not isinstance(value, str) or not value:
            raise ToolError(f"MCP credential reference for {name} is unavailable.")
        return value
    # ``read_server_configs`` validates references before a manager is
    # started.  Keep this guard for programmatically constructed configs.
    raise ToolError(f"MCP environment reference for {name} is invalid.")


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
