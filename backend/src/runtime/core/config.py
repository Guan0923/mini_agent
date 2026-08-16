"""Runtime limits kept separate from CLI argument parsing."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, init=False)
class RunnerSettings:
    max_transport_retries: int = 5
    max_tool_calls: int = 32
    log_full_messages: bool = True

    def __init__(
        self,
        max_transport_retries: int = 5,
        max_tool_calls: int = 32,
        log_full_messages: bool = True,
    ) -> None:
        values = {
            "max_transport_retries": max_transport_retries,
            "max_tool_calls": max_tool_calls,
            "log_full_messages": log_full_messages,
        }
        for name, value in values.items():
            object.__setattr__(self, name, value)
        self.__post_init__()

    def __post_init__(self) -> None:
        if self.max_transport_retries < 0:
            raise ValueError("max_transport_retries must be zero or greater.")
        if not isinstance(self.max_tool_calls, int) or isinstance(self.max_tool_calls, bool):
            raise ValueError("max_tool_calls must be an integer.")
        if not 1 <= self.max_tool_calls <= 1000:
            raise ValueError("max_tool_calls must be between 1 and 1000.")
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


def log_full_messages_from_toml(config_path: Path) -> bool:
    """Read the diagnostic policy from the sole client TOML configuration."""

    from backend.configuration import load_config, section

    raw = section(load_config(config_path), "runtime").get("log_full_messages", True)
    if isinstance(raw, bool):
        return raw
    raise ValueError("runtime.log_full_messages must be boolean.")
