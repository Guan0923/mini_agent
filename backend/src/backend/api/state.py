"""Shared API runtime state: config resolution, isolated client paths, chat workspace.

This module deliberately does NOT import the benchmark harness; benchmark
concerns live in the separately mounted benchmark sub-application.
"""

from __future__ import annotations

import json
import os
import time
import tomllib
from pathlib import Path

from backend.configuration import ClientPaths, atomic_write_text

REPO_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_DATA_ROOT = REPO_ROOT / "webapp-data"


def resolve_config_path() -> Path:
    """Locate the model config: MINI_AGENT_CONFIG env, else ~/mini_agent/config.toml."""
    env = os.environ.get("MINI_AGENT_CONFIG")
    if env:
        return Path(env)
    return Path.home() / "mini_agent" / "config.toml"


def activate_client_paths(paths: ClientPaths) -> None:
    """Redirect the application factory's client paths into the isolated data root."""
    import backend.runtime.application.factory as factory

    factory.client_paths = lambda: paths


def _read_toml(path: Path) -> dict:
    with path.open("rb") as handle:
        return tomllib.load(handle)


def _to_toml(values: dict[str, dict]) -> str:
    lines: list[str] = []
    for table, entries in values.items():
        lines.append(f"[{table}]")
        for key, value in entries.items():
            if isinstance(value, bool):
                rendered = "true" if value else "false"
            elif isinstance(value, int):
                rendered = str(value)
            else:
                rendered = json.dumps(str(value), ensure_ascii=False)
            lines.append(f"{key} = {rendered}")
        lines.append("")
    return "\n".join(lines)


def seed_client_config(paths: ClientPaths, source_config: Path | None) -> None:
    """Seed config.toml from the user's config, minus sync, with full logging."""
    values = _read_toml(source_config) if source_config is not None and source_config.exists() else {}
    normalized = {name: dict(value) for name, value in values.items() if isinstance(value, dict)}
    sync = dict(normalized.get("sync", {}))
    sync.pop("url", None)
    sync.pop("token", None)
    sync.setdefault("device_id", f"web_{int(time.time())}_{id(normalized)}")
    normalized["sync"] = sync
    runtime = dict(normalized.get("runtime", {}))
    runtime["log_full_messages"] = True
    normalized["runtime"] = runtime
    atomic_write_text(paths.config_file, _to_toml(normalized))


class WebAppState:
    """Shared runtime state for the main chat backend.

    Uses the real client home (``~/mini_agent``) — the same store the TUI writes
    to — so the web and terminal clients share one session/log/skill history.
    """

    def __init__(self, data_root: Path = DEFAULT_DATA_ROOT) -> None:
        self.data_root = data_root
        self.paths = ClientPaths.from_home()
        self.paths.ensure()
        activate_client_paths(self.paths)
        self.config_path = self.paths.config_file
        self.chat_workspace = data_root / "chat-workspace"
        self.chat_workspace.mkdir(parents=True, exist_ok=True)
