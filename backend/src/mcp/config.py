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
_ENV_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SENSITIVE_ENV_KEY_PATTERN = re.compile(
    r"(?:^|[_-])(?:api[_-]?key|auth(?:orization|entication)?|credential|password|passwd|private[_-]?key|secret|token)(?:$|[_-])",
    re.IGNORECASE,
)
_ENV_REFERENCE_PATTERNS = (
    re.compile(r"^env://(?P<name>[A-Za-z_][A-Za-z0-9_]*)$"),
    re.compile(r"^env:(?P<name>[A-Za-z_][A-Za-z0-9_]*)$"),
    re.compile(r"^\$\{(?P<name>[A-Za-z_][A-Za-z0-9_]*)\}$"),
)
_KEYRING_REFERENCE_PATTERN = re.compile(r"^keyring://(?P<service>[A-Za-z0-9_.-]+)/(?P<account>[A-Za-z0-9_.@-]+)$")


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


@dataclass(frozen=True, repr=False)
class McpServerConfig:
    name: str
    command: str
    args: tuple[str, ...] = ()
    cwd: str | None = None
    env: dict[str, str] | None = None
    enabled: bool = True
    env_refs: dict[str, str] | None = None

    def __repr__(self) -> str:
        """Keep accidental diagnostics from printing environment values."""

        names = sorted((self.env or {}).keys())
        references = sorted((self.env_refs or {}).keys())
        return (
            "McpServerConfig("
            f"name={self.name!r}, command={self.command!r}, args={self.args!r}, cwd={self.cwd!r}, "
            f"env_names={names!r}, env_ref_names={references!r}, enabled={self.enabled!r})"
        )


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
        merged = {item.name: item for item in self.global_servers if item.enabled}
        if include_project:
            for item in self.project_servers:
                if item.enabled:
                    merged[item.name] = item
                else:
                    # A project-level disabled entry intentionally masks a
                    # same-named user server instead of allowing the global
                    # definition to leak back into the effective plan.
                    merged.pop(item.name, None)
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

    # User-owned MCP files are part of the durable snapshot.  Sensitive
    # values must therefore be references to a process environment or OS
    # credential store, never plaintext in ``servers.toml``.  Project MCP
    # remains a separately trusted, backward-compatible input because it is
    # not copied into the user snapshot.
    global_servers = read_server_configs(paths.mcp_file, reject_plaintext_secrets=True)
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
        names = sorted({*(server.env or {}).keys(), *(server.env_refs or {}).keys()})
        lines.append(f"Environment names: {', '.join(names) if names else '(none)'}")
    return "\n".join(lines)


def read_server_configs(path: Path, *, reject_plaintext_secrets: bool = False) -> tuple[McpServerConfig, ...]:
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
        cwd, env, enabled = value.get("cwd"), value.get("env"), value.get("enabled", True)
        env_refs = value.get("env_refs")
        if (
            not isinstance(command, str)
            or not command.strip()
            or not isinstance(args, list)
            or not all(isinstance(item, str) for item in args)
            or not isinstance(enabled, bool)
        ):
            raise ToolError(f"{path}: servers.{name} requires command and string args.")
        if cwd is not None and not isinstance(cwd, str):
            raise ToolError(f"{path}: servers.{name}.cwd must be a string.")
        if env is not None and (
            not isinstance(env, dict)
            or not all(isinstance(key, str) and isinstance(item, str) for key, item in env.items())
        ):
            raise ToolError(f"{path}: servers.{name}.env must contain only string values.")
        if env_refs is not None and (
            not isinstance(env_refs, dict)
            or not all(isinstance(key, str) and isinstance(item, str) for key, item in env_refs.items())
        ):
            raise ToolError(f"{path}: servers.{name}.env_refs must contain only string values.")

        plain_values: dict[str, str] = {}
        references: dict[str, str] = dict(env_refs) if isinstance(env_refs, dict) else {}
        for key, item in (dict(env) if isinstance(env, dict) else {}).items():
            if not _ENV_NAME_PATTERN.fullmatch(key):
                raise ToolError(f"{path}: servers.{name}.env contains an invalid environment name.")
            reference = _parse_secret_reference(item)
            if reference is not None:
                references[key] = reference
                continue
            if reject_plaintext_secrets and _SENSITIVE_ENV_KEY_PATTERN.search(key):
                raise ToolError(f"{path}: servers.{name}.env.{key} is sensitive; use env_refs or an env:// reference.")
            plain_values[key] = item
        for key, reference in references.items():
            if not isinstance(key, str) or not _ENV_NAME_PATTERN.fullmatch(key):
                raise ToolError(f"{path}: servers.{name}.env_refs contains an invalid environment name.")
            if _parse_secret_reference(reference) is None:
                raise ToolError(
                    f"{path}: servers.{name}.env_refs.{key} must use env://, env:, ${{NAME}}, or keyring://."
                )
        result.append(
            McpServerConfig(
                name,
                command,
                tuple(args),
                cwd,
                plain_values or None,
                enabled,
                references or None,
            )
        )
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
            "env_refs": dict(sorted((server.env_refs or {}).items())),
            "enabled": server.enabled,
        }
        for server in servers
    ]
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _parse_secret_reference(value: str) -> str | None:
    """Validate a non-secret reference without resolving or exposing it."""

    for pattern in _ENV_REFERENCE_PATTERNS:
        match = pattern.fullmatch(value)
        if match is not None:
            return f"env://{match.group('name')}"
    match = _KEYRING_REFERENCE_PATTERN.fullmatch(value)
    if match is not None:
        return value
    return None
