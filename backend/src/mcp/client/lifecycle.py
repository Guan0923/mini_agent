"""Configuration loading and application-owned MCP resource lifecycle."""

from __future__ import annotations

from concurrent.futures import TimeoutError as FutureTimeoutError
from pathlib import Path

from backend.domain import safe_error_message
from backend.jobs import JobRegistry, JobScope
from backend.tools import Tool, ToolError

from ..config import McpServerConfig, McpSettings, read_server_configs, valid_tool_name
from .adapters import _handler
from .manager import ExternalMcpResources


def _manager_type():
    # Resolve through the public module at call time so callers can inject a
    # manager implementation exactly as they could before this module split.
    from . import ExternalMcpManager

    return ExternalMcpManager


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
) -> ExternalMcpResources:
    del project_file
    return start_external_tools(
        read_server_configs(global_file, reject_plaintext_secrets=True),
        settings,
        job_registry=job_registry,
        job_scope=job_scope,
    )


def start_external_tools(
    configs: tuple[McpServerConfig, ...],
    settings: McpSettings | None = None,
    *,
    job_registry: JobRegistry | None = None,
    job_scope: JobScope | None = None,
) -> ExternalMcpResources:
    if not configs:
        return ExternalMcpResources()
    try:
        manager_kwargs = {
            "job_registry": job_registry,
            "job_scope": job_scope,
        }
        manager_kwargs = {name: value for name, value in manager_kwargs.items() if value is not None}
        manager_type = _manager_type()
        manager = (
            manager_type(configs, **manager_kwargs)
            if settings is None
            else manager_type(configs, settings, **manager_kwargs)
        )
    except FutureTimeoutError as exc:
        raise ToolError(safe_error_message(exc)) from exc
    except Exception as exc:
        raise ToolError(safe_error_message(exc)) from exc

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
                        trace_origin={"kind": "mcp", "server": server.name, "tool": definition_name},
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
