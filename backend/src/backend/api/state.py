"""Shared API runtime state: config resolution, isolated client paths, chat workspace.

This module deliberately does NOT import the benchmark harness; benchmark
concerns live in the separately mounted benchmark sub-application.
"""

from __future__ import annotations

import json
import time
import tomllib
from pathlib import Path

from backend.configuration import ClientPaths, atomic_write_text

from .auth_mail import NullMailer, SMTPMailer, SMTPSettings
from .auth_store import AuthStore

REPO_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_DATA_ROOT = REPO_ROOT / "webapp-data"


def resolve_config_path() -> Path:
    """Locate the server's client-owned TOML configuration."""
    return Path.home() / "mini_agent" / "config.toml"


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


def seed_client_config(paths: ClientPaths, source_config: Path | None, *, device_id: str | None = None) -> None:
    """Seed only runtime/model settings; never copy mail, web or sync secrets."""
    values = _read_toml(source_config) if source_config is not None and source_config.exists() else {}
    allowed = {"model", "runtime", "mcp", "skills", "subagents"}
    normalized = {
        name: {key: item for key, item in value.items() if isinstance(key, str) and isinstance(item, (str, int, bool))}
        for name, value in values.items()
        if name in allowed and isinstance(value, dict)
    }
    normalized["sync"] = {"device_id": device_id or f"web_{int(time.time())}_{id(normalized)}"}
    runtime = dict(normalized.get("runtime", {}))
    runtime["log_full_messages"] = True
    normalized["runtime"] = runtime
    atomic_write_text(paths.config_file, _to_toml(normalized))


class WebAppState:
    """Shared runtime state for the main chat backend.

    Keeps the server configuration in ``~/mini_agent`` while assigning every
    authenticated web user a separate client/session/workspace root.
    """

    def __init__(self, data_root: Path = DEFAULT_DATA_ROOT, *, mailer=None) -> None:
        self.data_root = data_root
        self.paths = ClientPaths.from_home()
        self.paths.ensure()
        self.config_path = self.paths.config_file
        self.chat_workspace = data_root / "chat-workspace"
        self.chat_workspace.mkdir(parents=True, exist_ok=True)
        self.auth = AuthStore(data_root / "auth.sqlite3")
        if mailer is not None:
            self.mailer = mailer
        else:
            from backend.configuration import load_config, section

            try:
                config = load_config(self.config_path)
                settings = SMTPSettings.from_config(dict(section(config, "email")))
            except Exception:
                settings = None
            self.mailer = SMTPMailer(settings) if settings is not None else NullMailer()
        from .auth_service import AuthService

        self.auth_service = AuthService(self)

    def user_paths(self, user_id: str) -> ClientPaths:
        from .user_data import user_paths

        return user_paths(self.data_root, user_id, self.config_path)

    def user_workspace(self, user_id: str) -> Path:
        from .user_data import user_workspace

        return user_workspace(self.data_root, user_id)

    def user_benchmark_root(self, user_id: str) -> Path:
        from .user_data import user_benchmark_root

        return user_benchmark_root(self.data_root, user_id)

    def close(self) -> None:
        close = getattr(self.mailer, "close", None)
        if callable(close):
            close()
