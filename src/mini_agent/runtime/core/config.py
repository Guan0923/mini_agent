"""Runtime limits kept separate from CLI argument parsing."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from mini_agent.domain import StrategyPolicy


@dataclass(frozen=True)
class RunnerSettings:
    max_retries: int = 1
    max_model_repairs: int = 1
    max_transport_retries: int = 2
    max_tool_recoveries: int = 2
    max_actions: int = 8
    max_replans: int = 2
    strategy: StrategyPolicy = "auto"
    log_full_messages: bool = True

    def __post_init__(self) -> None:
        if self.max_retries < 0:
            raise ValueError("max_retries must be zero or greater.")
        if self.max_model_repairs < 0:
            raise ValueError("max_model_repairs must be zero or greater.")
        if self.max_transport_retries < 0:
            raise ValueError("max_transport_retries must be zero or greater.")
        if self.max_tool_recoveries < 0:
            raise ValueError("max_tool_recoveries must be zero or greater.")
        if self.max_actions < 1:
            raise ValueError("max_actions must be at least one.")
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
                values[key.strip()] = value.strip().strip("\"'")
    values.update(dict(os.environ if environ is None else environ))
    raw = values.get("LOG_FULL_MESSAGES", "true").strip().lower()
    if raw == "true":
        return True
    if raw == "false":
        return False
    raise ValueError("LOG_FULL_MESSAGES must be true or false.")
