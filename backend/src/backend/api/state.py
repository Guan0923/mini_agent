"""Shared API runtime state for the Web backend."""

from __future__ import annotations

import json
import os
import time
import tomllib
from pathlib import Path

from backend.configuration import ClientPaths, atomic_write_text

from .auth.mail import NullMailer, SMTPMailer, SMTPSettings
from .auth_store import AuthStore

REPO_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_DATA_ROOT = Path.home() / "mini_agent" / "runtime" / "web"


def resolve_config_path() -> Path:
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
    """Compatibility migration helper; Web no longer calls this per user."""
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
    """Shared auth/settings/runtime state for the Web backend."""

    def __init__(self, data_root: Path = DEFAULT_DATA_ROOT, *, mailer=None) -> None:
        self.data_root = data_root
        self.paths = ClientPaths.from_home()
        self.paths.ensure()
        self.config_path = self.paths.config_file
        self.chat_workspace = data_root / "chat-workspace"
        self.chat_workspace.mkdir(parents=True, exist_ok=True)
        auth_root = Path.home() / "mini_agent" if data_root == DEFAULT_DATA_ROOT else data_root
        self.auth = AuthStore(auth_root / "auth.sqlite3")
        self.settings = self.auth
        database_url = os.environ.get("DATABASE_URL", "").strip()
        if database_url:
            try:
                from backend.storage.postgres.settings import PostgresSettingsRepository

                self.settings = PostgresSettingsRepository(database_url)
            except Exception:
                self.settings = self.auth
        if mailer is not None:
            self.mailer = mailer
        else:
            from backend.configuration import load_config, section

            try:
                config = load_config(self.config_path)
                mail_settings = SMTPSettings.from_config(dict(section(config, "email")))
            except Exception:
                mail_settings = None
            self.mailer = SMTPMailer(mail_settings) if mail_settings is not None else NullMailer()
        from .auth.service import AuthService

        self.auth_service = AuthService(self)

    def user_paths(self, user_id: str) -> ClientPaths:
        from .user_data import user_paths

        return user_paths(self.data_root, user_id)

    def user_workspace(self, user_id: str) -> Path:
        from .user_data import user_workspace

        return user_workspace(self.data_root, user_id)

    def user_benchmark_root(self, user_id: str) -> Path:
        from .user_data import user_benchmark_root

        return user_benchmark_root(self.data_root, user_id)

    def settings_for_user(self, user_id: str) -> dict[str, object]:
        identity = self.auth.user_by_id(user_id)
        email = identity.email if identity is not None else ""
        return self.settings.settings_for_user(user_id, email=email)

    def model_config_for_user(self, user_id: str):
        return self.settings.model_config_for_user(user_id)

    def agent_config_for_user(self, user_id: str) -> dict[str, object]:
        return self.settings.agent_config_for_user(user_id)

    def agent_preferences_for_user(self, user_id: str) -> str:
        return self.settings.agent_preferences_for_user(user_id)

    def runtime_config_for_user(self, user_id: str) -> dict[str, object]:
        return self.settings.runtime_config_for_user(user_id)

    def close(self) -> None:
        close = getattr(self.mailer, "close", None)
        if callable(close):
            close()
