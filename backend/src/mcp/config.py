"""Validated user-level MCP configuration."""

from __future__ import annotations

import math
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from backend.configuration import ClientPaths, ConfigurationError, section
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
    health_failure_threshold: int = 3
    rebuild_failure_threshold: int = 3

    @classmethod
    def from_config(cls, values: Mapping[str, object]) -> McpSettings:
        configured = section(values, "mcp")
        return cls(
            initialization_timeout_seconds=_positive_number(configured, "initialization_timeout_seconds", 15.0),
            call_timeout_seconds=_positive_number(configured, "call_timeout_seconds", 60.0),
            shutdown_timeout_seconds=_positive_number(configured, "shutdown_timeout_seconds", 5.0),
            health_failure_threshold=_positive_integer(configured, "health_failure_threshold", 3),
            rebuild_failure_threshold=_positive_integer(configured, "rebuild_failure_threshold", 3),
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
    """A validated, side-effect-free description of user MCP configuration."""

    global_servers: tuple[McpServerConfig, ...]

    def effective_servers(self) -> tuple[McpServerConfig, ...]:
        return tuple(sorted((item for item in self.global_servers if item.enabled), key=lambda item: item.name))


class McpTrustStore:
    """Persist only workspace/config digests, never MCP configuration values."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def is_trusted(self, plan: McpConfigPlan) -> bool:
        # Project MCP trust is obsolete: only user-level MCP servers exist.
        return True

    def trust(self, plan: McpConfigPlan) -> None:
        return

    def _read(self) -> dict[str, dict[str, str]]:
        return {}


def prepare_mcp_plan(paths: ClientPaths, workspace: Path | None = None) -> McpConfigPlan:
    """Parse and validate the user-level MCP file without starting a process.

    ``workspace`` is accepted for compatibility and ignored: the user-level
    ``servers.toml`` is the single source of external MCP servers.
    """

    # User-owned MCP files are part of the durable snapshot.  Sensitive
    # values must therefore be references to a process environment or OS
    # credential store, never plaintext in ``servers.toml``.
    del workspace
    global_servers = read_server_configs(paths.mcp_file, reject_plaintext_secrets=True)
    return McpConfigPlan(global_servers=global_servers)


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


def _positive_integer(values: Mapping[str, object], name: str, default: int) -> int:
    raw = values.get(name, default)
    if isinstance(raw, bool) or not isinstance(raw, int) or raw <= 0:
        raise ConfigurationError(f"mcp.{name} must be a positive integer.")
    return raw


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
