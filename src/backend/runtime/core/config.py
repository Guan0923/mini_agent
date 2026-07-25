"""Runtime limits kept separate from CLI argument parsing."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from backend.domain import StrategyPolicy

_UNSET = object()


@dataclass(frozen=True, init=False)
class RunnerSettings:
    max_retries: int = 1
    max_model_repairs: int = 2
    max_transport_retries: int = 2
    max_tool_recoveries: int = 2
    max_model_turns: int = 8
    max_tool_calls: int = 32
    max_replans: int = 2
    strategy: StrategyPolicy = "auto"
    log_full_messages: bool = True
    max_actions: int = field(default=32, repr=False, compare=False)

    def __init__(
        self,
        max_retries: int = 1,
        max_model_repairs: int = 2,
        max_transport_retries: int = 2,
        max_tool_recoveries: int = 2,
        max_actions: int | object = _UNSET,
        max_replans: int = 2,
        strategy: StrategyPolicy = "auto",
        log_full_messages: bool = True,
        *,
        max_model_turns: int = 8,
        max_tool_calls: int | object = _UNSET,
    ) -> None:
        if max_actions is not _UNSET and max_tool_calls is not _UNSET:
            raise ValueError("max_actions and max_tool_calls cannot be used together.")
        if max_actions is not _UNSET and max_actions < 1:
            raise ValueError("max_actions must be at least one.")
        resolved_tool_calls = 32
        if max_actions is not _UNSET:
            resolved_tool_calls = max_actions
        elif max_tool_calls is not _UNSET:
            resolved_tool_calls = max_tool_calls
        values = {
            "max_retries": max_retries,
            "max_model_repairs": max_model_repairs,
            "max_transport_retries": max_transport_retries,
            "max_tool_recoveries": max_tool_recoveries,
            "max_model_turns": max_model_turns,
            "max_tool_calls": resolved_tool_calls,
            "max_replans": max_replans,
            "strategy": strategy,
            "log_full_messages": log_full_messages,
            "max_actions": resolved_tool_calls,
        }
        for name, value in values.items():
            object.__setattr__(self, name, value)
        self.__post_init__()

    def __post_init__(self) -> None:
        if self.max_retries < 0:
            raise ValueError("max_retries must be zero or greater.")
        if self.max_model_repairs < 0:
            raise ValueError("max_model_repairs must be zero or greater.")
        if self.max_transport_retries < 0:
            raise ValueError("max_transport_retries must be zero or greater.")
        if self.max_tool_recoveries < 0:
            raise ValueError("max_tool_recoveries must be zero or greater.")
        if self.max_model_turns < 1:
            raise ValueError("max_model_turns must be at least one.")
        if self.max_tool_calls < 1:
            raise ValueError("max_tool_calls must be at least one.")
        if self.max_replans < 0:
            raise ValueError("max_replans must be zero or greater.")
        if self.strategy not in {"auto", "reactive", "dynamic_replan"}:
            raise ValueError("strategy must be 'auto', 'reactive', or 'dynamic_replan'.")
        if not isinstance(self.log_full_messages, bool):
            raise ValueError("log_full_messages must be boolean.")


def log_full_messages_from_env(env_path: Path, environ: Mapping[str, str] | None = None) -> bool:
    """Read the local diagnostic-content policy without loading provider credentials."""

    values: dict[str, str] = {}
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            if key.strip():
                values[key.strip()] = value.strip("\"'")
    values.update(dict(os.environ if environ is None else environ))
    raw = values.get("LOG_FULL_MESSAGES", "true").strip().lower()
    if raw == "true":
        return True
    if raw == "false":
        return False
    raise ValueError("LOG_FULL_MESSAGES must be true or false.")


def database_url_from_env(env_path: Path, environ: Mapping[str, str] | None = None) -> str:
    """Read the required PostgreSQL URL, allowing process values to override .env."""

    values: dict[str, str] = {}
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            if key.strip():
                values[key.strip()] = value.strip("\"'")
    values.update(dict(os.environ if environ is None else environ))
    database_url = values.get("DATABASE_URL", "").strip()
    if not database_url:
        raise ValueError("DATABASE_URL must be configured for PostgreSQL storage.")
    return database_url
