"""Per-user SQLite settings database used by both Web and TUI clients."""

from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path

from backend.configuration import ClientPaths, UserConfigStore, validate_identity_id

from .auth.settings import AuthSettingsMixin

USER_SETTINGS_SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS user_provider_settings (
    user_id TEXT PRIMARY KEY,
    provider TEXT NOT NULL DEFAULT 'deepseek',
    protocol TEXT NOT NULL DEFAULT 'chat_completions',
    base_url TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    max_tokens INTEGER NOT NULL DEFAULT 8192,
    context_size INTEGER NOT NULL DEFAULT 1024000,
    tokenizer_model TEXT NOT NULL DEFAULT 'deepseek-ai/DeepSeek-V3',
    api_key_ciphertext TEXT NOT NULL DEFAULT '',
    provider_configs_json TEXT NOT NULL DEFAULT '[]',
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS sync_state (
    user_id TEXT PRIMARY KEY,
    local_revision INTEGER NOT NULL DEFAULT 0,
    uploaded_revision INTEGER NOT NULL DEFAULT 0,
    cloud_snapshot_id TEXT,
    status TEXT NOT NULL DEFAULT 'local_only',
    last_error TEXT NOT NULL DEFAULT '',
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS app_metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cloud_credentials (
    user_id TEXT PRIMARY KEY,
    token_ciphertext TEXT NOT NULL DEFAULT '',
    expires_at REAL NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS user_profiles (
    user_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL DEFAULT '',
    agent_preferences TEXT NOT NULL DEFAULT '',
    updated_at REAL NOT NULL
);
"""

AUTO_SAVE_RULES = frozenset({"idle_5m", "after_run", "hourly"})


class UserSettingsStore(AuthSettingsMixin):
    """Settings operations for exactly one authenticated user's ``user.db``."""

    def __init__(self, path: Path) -> None:
        path = Path(path)
        if path.name != "user.db":
            raise ValueError("User settings database must be named user.db.")
        validate_identity_id(path.parent.name, require_uuid=True)
        if path.parent.is_symlink() or path.is_symlink():
            raise ValueError("User settings paths cannot be symbolic links.")
        self.path = path
        self.config_store = UserConfigStore(path.with_name("config.toml"))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.config_store.ensure_defaults(
            {
                "profile": {"display_name": "", "agent_preferences": ""},
                "agent": {
                    "tone": "balanced",
                    "verbosity": "balanced",
                    "initiative": "balanced",
                    "custom_instructions": "",
                    "display_mode": "medium",
                    "timezone": "Asia/Shanghai",
                    "location_enabled": False,
                },
                "runtime": {"log_full_messages": True, "max_tool_calls": 32, "terminal_type": "cmd"},
                "capabilities": {"skills": True, "rag": False, "plugins": False, "mcp": False},
                "providers": {"active_id": ""},
                "sync": {
                    "auto_save_enabled": False,
                    "auto_save_rule": "idle_5m",
                    "device_id": f"web_{path.parent.name}",
                },
            }
        )
        with self._connection() as connection:
            connection.executescript(USER_SETTINGS_SCHEMA)
        self._migrate_legacy_profile()

    @contextmanager
    def _connection(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA busy_timeout = 10000")
        if immediate:
            connection.execute("BEGIN IMMEDIATE")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def ping(self) -> None:
        with self._connection() as connection:
            connection.execute("SELECT 1")

    def set_metadata(self, key: str, value: str) -> None:
        with self._connection(immediate=True) as connection:
            connection.execute(
                "INSERT INTO app_metadata(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def mark_dirty(self, user_id: str) -> int:
        now = time.time()
        with self._connection(immediate=True) as connection:
            row = connection.execute("SELECT local_revision FROM sync_state WHERE user_id=?", (user_id,)).fetchone()
            revision = (int(row[0]) if row is not None else 0) + 1
            connection.execute(
                """INSERT INTO sync_state
                (user_id,local_revision,uploaded_revision,status,last_error,updated_at)
                VALUES (?,?,0,'dirty','',?)
                ON CONFLICT(user_id) DO UPDATE SET
                    local_revision=excluded.local_revision,status='dirty',last_error='',updated_at=excluded.updated_at""",
                (user_id, revision, now),
            )
        return revision

    def sync_preferences_for_user(self, user_id: str) -> dict[str, object]:
        config = self.config_store.read()
        sync = config.get("sync")
        if isinstance(sync, Mapping) and ("auto_save_enabled" in sync or "auto_save_rule" in sync):
            rule = str(sync.get("auto_save_rule") or "idle_5m")
            return {
                "auto_save_enabled": bool(sync.get("auto_save_enabled", False)),
                "auto_save_rule": rule if rule in AUTO_SAVE_RULES else "idle_5m",
            }
        return {"auto_save_enabled": False, "auto_save_rule": "idle_5m"}

    def update_sync_preferences(self, user_id: str, values: Mapping[str, object]) -> dict[str, object]:
        current = self.sync_preferences_for_user(user_id)
        enabled = values.get("auto_save_enabled", current["auto_save_enabled"])
        if not isinstance(enabled, bool):
            raise ValueError("auto_save_enabled must be a boolean")
        rule = str(values.get("auto_save_rule", current["auto_save_rule"]) or "")
        if rule not in AUTO_SAVE_RULES:
            raise ValueError("auto_save_rule must be idle_5m, after_run, or hourly")
        self.config_store.update({"sync": {"auto_save_enabled": enabled, "auto_save_rule": rule}})
        self.mark_dirty(user_id)
        return {"auto_save_enabled": enabled, "auto_save_rule": rule}

    def sync_state_for_user(self, user_id: str) -> dict[str, object]:
        with self._connection() as connection:
            row = connection.execute(
                """SELECT local_revision,uploaded_revision,cloud_snapshot_id,status,last_error,updated_at
                FROM sync_state WHERE user_id=?""",
                (user_id,),
            ).fetchone()
        if row is None:
            return {
                "local_revision": 0,
                "uploaded_revision": 0,
                "cloud_snapshot_id": None,
                "status": "local_only",
                "last_error": "",
                "updated_at": None,
            }
        return {
            "local_revision": int(row[0]),
            "uploaded_revision": int(row[1]),
            "cloud_snapshot_id": str(row[2]) if row[2] else None,
            "status": str(row[3]),
            "last_error": str(row[4] or ""),
            "updated_at": float(row[5]),
        }

    def set_sync_status(self, user_id: str, status: str, *, error: str = "") -> None:
        now = time.time()
        with self._connection(immediate=True) as connection:
            connection.execute(
                """INSERT INTO sync_state
                (user_id,local_revision,uploaded_revision,status,last_error,updated_at)
                VALUES (?,0,0,?,?,?) ON CONFLICT(user_id) DO UPDATE SET
                status=excluded.status,last_error=excluded.last_error,updated_at=excluded.updated_at""",
                (user_id, status, error[:1000], now),
            )

    def mark_uploaded(self, user_id: str, snapshot_id: str, revision: int) -> None:
        now = time.time()
        with self._connection(immediate=True) as connection:
            connection.execute(
                """INSERT INTO sync_state
                (user_id,local_revision,uploaded_revision,cloud_snapshot_id,status,last_error,updated_at)
                VALUES (?,?,?,?, 'synced','',?) ON CONFLICT(user_id) DO UPDATE SET
                uploaded_revision=excluded.uploaded_revision,
                cloud_snapshot_id=excluded.cloud_snapshot_id,
                status=CASE WHEN sync_state.local_revision=excluded.uploaded_revision THEN 'synced' ELSE 'dirty' END,
                last_error='',updated_at=excluded.updated_at""",
                (user_id, revision, revision, snapshot_id, now),
            )


class PerUserSettingsRepository:
    """Dispatch the settings contract to ``<data_root>/<user_id>/user.db``."""

    _MUTATIONS = frozenset(
        {
            "update_profile",
            "update_agent_config",
            "update_runtime_config",
            "update_provider_config",
            "add_provider_config",
            "update_provider_config_by_id",
            "activate_provider_config",
            "delete_provider_config",
        }
    )

    def __init__(self, data_root: Path) -> None:
        data_root = Path(data_root)
        if data_root.is_symlink():
            raise ValueError("User settings root cannot be a symbolic link.")
        self.data_root = data_root.resolve()
        self.data_root.mkdir(parents=True, exist_ok=True)
        self._stores: dict[str, UserSettingsStore] = {}
        self._lock = threading.Lock()

    def _store(self, user_id: str) -> UserSettingsStore:
        validate_identity_id(user_id, require_uuid=True)
        with self._lock:
            store = self._stores.get(user_id)
            if store is None:
                if self.data_root.is_symlink():
                    raise ValueError("User settings root cannot be a symbolic link.")
                candidate = self.data_root / user_id
                if candidate.is_symlink():
                    raise ValueError("User settings path cannot be a symbolic link.")
                root = candidate.resolve()
                if root.parent != self.data_root:
                    raise ValueError("User settings path must remain inside the data root.")
                paths = ClientPaths(root)
                paths.ensure()
                store = UserSettingsStore(paths.user_db)
                self._stores[user_id] = store
            return store

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)

        def dispatch(user_id: str, *args, **kwargs):
            store = self._store(user_id)
            result = getattr(store, name)(user_id, *args, **kwargs)
            if name in self._MUTATIONS:
                store.mark_dirty(user_id)
            return result

        return dispatch

    def ping(self) -> None:
        self.data_root.mkdir(parents=True, exist_ok=True)

    def invalidate(self, user_id: str) -> None:
        """Drop a cached facade after an atomic snapshot restore.

        ``UserSettingsStore`` opens SQLite connections per operation, but its
        config/database facade is still cached by identity.  Removing it here
        guarantees the next request re-reads defaults and the restored TOML
        rather than retaining any in-memory assumptions from before restore.
        """

        validate_identity_id(user_id, require_uuid=True)
        with self._lock:
            self._stores.pop(user_id, None)

    def close(self) -> None:
        self._stores.clear()
