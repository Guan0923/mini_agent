"""MCP stdio parameter resolution, Tool handlers, and bounded result rendering."""

from __future__ import annotations

import json
import os
from typing import Any, Protocol

from backend.tools import ToolError

from ..config import McpServerConfig
from ..controlled_stdio import StdioServerParameters


class _McpCaller(Protocol):
    def call(self, server_name: str, tool_name: str, arguments: dict[str, Any]) -> str: ...


_MAX_RESULT_CHARS = 20_000


def _handler(manager: _McpCaller, server_name: str, tool_name: str):
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
