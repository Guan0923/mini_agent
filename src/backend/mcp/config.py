"""Validated MCP configuration and project trust persistence."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from backend.configuration import ClientPaths, ConfigurationError, atomic_write_text, section
from backend.tools import ToolError

_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")


@dataclass(frozen=True)
class McpSettings:
    """Finite time limits for external MCP lifecycle operations."""

    initialization_timeout_seconds: float = 15.0
    call_timeout_seconds: float = 60.0
    shutdown_timeout_seconds: float = 5.0

    @classmethod
    def from_config(cls, values: Mapping[str, object]) -> McpSettings:
        configured = section(values, "mcp")
        return cls(
            initialization_timeout_seconds=_positive_number(configured, "initialization_timeout_seconds", 15.0),
            call_timeout_seconds=_positive_number(configured, "call_timeout_seconds", 60.0),
            shutdown_timeout_seconds=_positive_number(configured, "shutdown_timeout_seconds", 5.0),
        )


@dataclass(frozen=True)
class McpServerConfig:
    name: str
    command: str
    args: tuple[str, ...] = ()
    cwd: str | None = None
    env: dict[str, str] | None = None


@dataclass(frozen=True)
class McpConfigPlan:
    """A validated, side-effect-free description of layered MCP configuration."""

    workspace_id: str
    project_digest: str | None
    global_servers: tuple[McpServerConfig, ...]
    project_servers: tuple[McpServerConfig, ...]

    @property
    def has_project_config(self) -> bool:
        return self.project_digest is not None

    def effective_servers(self, *, include_project: bool) -> tuple[McpServerConfig, ...]:
        merged = {item.name: item for item in self.global_servers}
        if include_project:
            merged.update({item.name: item for item in self.project_servers})
        return tuple(merged[name] for name in sorted(merged))


class McpTrustStore:
    """Persist only workspace/config digests, never MCP configuration values."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def is_trusted(self, plan: McpConfigPlan) -> bool:
        if not plan.has_project_config:
            return True
        record = self._read().get(plan.workspace_id)
        return isinstance(record, dict) and record.get("config_sha256") == plan.project_digest

    def trust(self, plan: McpConfigPlan) -> None:
        if plan.project_digest is None:
            return
        entries = self._read()
        entries[plan.workspace_id] = {
            "config_sha256": plan.project_digest,
            "trusted_at": datetime.now(UTC).isoformat(),
        }
        lines: list[str] = []
        for workspace_id in sorted(entries):
            record = entries[workspace_id]
            digest = record.get("config_sha256")
            trusted_at = record.get("trusted_at")
            if not isinstance(digest, str) or not isinstance(trusted_at, str):
                continue
            lines.extend(
                (
                    f"[workspaces.{json.dumps(workspace_id)}]",
                    f"config_sha256 = {json.dumps(digest)}",
                    f"trusted_at = {json.dumps(trusted_at)}",
                    "",
                )
            )
        atomic_write_text(self.path, "\n".join(lines))

    def _read(self) -> dict[str, dict[str, str]]:
        if not self.path.exists():
            return {}
        try:
            with self.path.open("rb") as handle:
                values = tomllib.load(handle)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise ConfigurationError(f"Invalid MCP trust store {self.path}: {exc}") from exc
        workspaces = values.get("workspaces", {})
        if not isinstance(workspaces, dict):
            raise ConfigurationError(f"{self.path}: [workspaces] must be a table.")
        return {
            str(key): {str(name): str(value) for name, value in record.items()}
            for key, record in workspaces.items()
            if isinstance(record, dict)
        }


def prepare_mcp_plan(paths: ClientPaths, workspace: Path) -> McpConfigPlan:
    """Parse and validate all MCP files without starting a process."""

    global_servers = read_server_configs(paths.mcp_file)
    project_file = workspace / ".mini_agent" / "mcp.toml"
    project_servers = read_server_configs(project_file)
    project_digest = _server_digest(project_servers) if project_file.exists() else None
    return McpConfigPlan(
        workspace_id=_workspace_id(workspace),
        project_digest=project_digest,
        global_servers=global_servers,
        project_servers=project_servers,
    )


def describe_project_servers(plan: McpConfigPlan) -> str:
    """Render a review that deliberately omits configured environment values."""

    lines = ["PROJECT MCP REVIEW"]
    if not plan.project_servers:
        lines.append("The project MCP file defines no servers.")
    for server in plan.project_servers:
        lines.append(f"\nServer: {server.name}")
        lines.append(f"Command: {server.command}")
        lines.append(f"Args: {json.dumps(list(server.args), ensure_ascii=False)}")
        lines.append(f"Cwd: {server.cwd or '(inherited)'}")
        names = sorted((server.env or {}).keys())
        lines.append(f"Environment names: {', '.join(names) if names else '(none)'}")
    return "\n".join(lines)


def read_server_configs(path: Path) -> tuple[McpServerConfig, ...]:
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
        if not _NAME_PATTERN.fullmatch(name):
            raise ToolError(f"{path}: server name {name!r} must use letters, digits, '_' or '-'.")
        command, args = value.get("command"), value.get("args", [])
        cwd, env = value.get("cwd"), value.get("env")
        if (
            not isinstance(command, str)
            or not command.strip()
            or not isinstance(args, list)
            or not all(isinstance(item, str) for item in args)
        ):
            raise ToolError(f"{path}: servers.{name} requires command and string args.")
        if cwd is not None and not isinstance(cwd, str):
            raise ToolError(f"{path}: servers.{name}.cwd must be a string.")
        if env is not None and (
            not isinstance(env, dict)
            or not all(isinstance(key, str) and isinstance(item, str) for key, item in env.items())
        ):
            raise ToolError(f"{path}: servers.{name}.env must contain only string values.")
        result.append(McpServerConfig(name, command, tuple(args), cwd, dict(env) if isinstance(env, dict) else None))
    return tuple(result)


def valid_tool_name(value: str) -> bool:
    return len(value) <= 64 and _NAME_PATTERN.fullmatch(value) is not None


def _positive_number(values: Mapping[str, object], name: str, default: float) -> float:
    raw = values.get(name, default)
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        raise ConfigurationError(f"mcp.{name} must be a finite positive number.")
    resolved = float(raw)
    if not math.isfinite(resolved) or resolved <= 0:
        raise ConfigurationError(f"mcp.{name} must be a finite positive number.")
    return resolved


def _workspace_id(workspace: Path) -> str:
    normalized = os.path.normcase(str(workspace.resolve())).replace("\\", "/")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _server_digest(servers: tuple[McpServerConfig, ...]) -> str:
    payload = [
        {
            "name": server.name,
            "command": server.command,
            "args": list(server.args),
            "cwd": server.cwd,
            "env": dict(sorted((server.env or {}).items())),
        }
        for server in servers
    ]
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()
