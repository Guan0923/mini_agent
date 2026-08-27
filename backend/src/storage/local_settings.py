"""Singleton local settings and encrypted provider storage."""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path

from backend.configuration import LocalConfigStore
from backend.storage.settings_contract import (
    DEFAULT_AGENT_CONFIG,
    DEFAULT_CAPABILITY_CONFIG,
    DEFAULT_PROFILE,
    DEFAULT_PROVIDER_CONFIG,
    DEFAULT_RUNTIME_CONFIG,
    DEFAULT_SANDBOX_CONFIG,
    normalize_agent_config,
    normalize_provider_config,
    normalize_runtime_config,
    normalize_sandbox_config,
    timezone_options,
)

from .local_crypto import decrypt_secret, encrypt_secret

LOCAL_SETTINGS_SCHEMA = """
CREATE TABLE IF NOT EXISTS provider_settings (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    provider_configs_json TEXT NOT NULL DEFAULT '[]',
    updated_at REAL NOT NULL
);
"""


class LocalSettingsStore:
    """Own settings for the one local Mini-Agent installation."""

    def __init__(self, path: Path, config_path: Path) -> None:
        self.path = Path(path)
        if self.path.name != "state.db" or self.path.parent.name != "runtime":
            raise ValueError("Local settings database must be runtime/state.db.")
        if self.path.is_symlink() or self.path.parent.is_symlink():
            raise ValueError("Local settings paths cannot be symbolic links.")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.config_store = LocalConfigStore(config_path)
        self.config_store.ensure_defaults(
            {
                "profile": {"display_name": "本地用户", "agent_preferences": ""},
                "agent": dict(DEFAULT_AGENT_CONFIG),
                "runtime": {"log_full_messages": True, **DEFAULT_RUNTIME_CONFIG},
                "sandbox": dict(DEFAULT_SANDBOX_CONFIG),
                "capabilities": dict(DEFAULT_CAPABILITY_CONFIG),
            }
        )
        with self._connection() as connection:
            connection.executescript(LOCAL_SETTINGS_SCHEMA)

    @contextmanager
    def _connection(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
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

    def close(self) -> None:
        return None

    def profile(self) -> dict[str, str]:
        raw = self.config_store.read().get("profile")
        source = raw if isinstance(raw, Mapping) else DEFAULT_PROFILE
        return {
            "display_name": str(source.get("display_name") or "本地用户"),
            "agent_preferences": str(source.get("agent_preferences") or ""),
        }

    def update_profile(self, *, display_name: str, agent_preferences: str) -> dict[str, str]:
        name = str(display_name).strip()
        preferences = str(agent_preferences).strip()
        if not name:
            raise ValueError("display_name must not be empty")
        if len(name) > 80 or len(preferences) > 4000:
            raise ValueError("profile is too long")
        result = {"display_name": name, "agent_preferences": preferences}
        self.config_store.update({"profile": result})
        return result

    def agent_config(self) -> dict[str, object]:
        raw = self.config_store.read().get("agent")
        return normalize_agent_config(DEFAULT_AGENT_CONFIG, raw if isinstance(raw, Mapping) else {})

    def update_agent_config(self, values: Mapping[str, object]) -> dict[str, object]:
        result = normalize_agent_config(self.agent_config(), values)
        self.config_store.update({"agent": result})
        return result

    def runtime_config(self) -> dict[str, object]:
        raw = self.config_store.read().get("runtime")
        source = raw if isinstance(raw, Mapping) else {}
        return {
            "log_full_messages": bool(source.get("log_full_messages", True)),
            **normalize_runtime_config(DEFAULT_RUNTIME_CONFIG, source),
        }

    def update_runtime_config(self, values: Mapping[str, object]) -> dict[str, object]:
        result = normalize_runtime_config(self.runtime_config(), values)
        current = self.runtime_config()
        current.update(result)
        self.config_store.update({"runtime": current})
        return result

    def sandbox_config(self) -> dict[str, object]:
        raw = self.config_store.read().get("sandbox")
        result = normalize_sandbox_config(raw if isinstance(raw, Mapping) else DEFAULT_SANDBOX_CONFIG)
        if not isinstance(raw, Mapping) or raw.get("policy_version") != 2 or raw.get("enabled") is not True:
            self.config_store.update({"sandbox": result})
        return result

    def update_sandbox_config(self, values: Mapping[str, object]) -> dict[str, object]:
        result = normalize_sandbox_config(self.sandbox_config(), values)
        self.config_store.update({"sandbox": result})
        return result

    def capability_config(self) -> dict[str, object]:
        raw = self.config_store.read().get("capabilities")
        return dict(raw) if isinstance(raw, Mapping) else dict(DEFAULT_CAPABILITY_CONFIG)

    def agent_preferences(self) -> str:
        agent = self.agent_config()
        profile = self.profile()
        parts = [
            f"Preferred tone: {agent['tone']}" if agent["tone"] != "balanced" else "",
            f"Preferred verbosity: {agent['verbosity']}" if agent["verbosity"] != "balanced" else "",
            f"Preferred initiative: {agent['initiative']}" if agent["initiative"] != "balanced" else "",
            str(agent["custom_instructions"]),
            profile["agent_preferences"],
        ]
        return "\n".join(item for item in parts if item).strip()

    @staticmethod
    def _public_provider(record: Mapping[str, object]) -> dict[str, object]:
        return {
            "id": str(record.get("id") or ""),
            "is_active": bool(record.get("is_active")),
            "provider_name": str(record.get("provider_name") or "default"),
            "protocol": str(record.get("protocol") or "chat_completions"),
            "base_url": str(record.get("base_url") or ""),
            "model": str(record.get("model") or ""),
            "max_tokens": int(record.get("max_tokens") or 8192),
            "context_size": int(record.get("context_size") or 1_024_000),
            "tokenizer_model": str(record.get("tokenizer_model") or ""),
            "api_key_configured": bool(record.get("api_key_ciphertext")),
        }

    def _provider_records(self, connection=None) -> list[dict[str, object]]:
        def read(active_connection) -> list[dict[str, object]]:
            row = active_connection.execute(
                "SELECT provider_configs_json FROM provider_settings WHERE id = 1"
            ).fetchone()
            if row is None:
                return []
            try:
                value = json.loads(str(row[0] or "[]"))
            except (TypeError, ValueError):
                return []
            return (
                [dict(item) for item in value if isinstance(item, dict) and item.get("id")]
                if isinstance(value, list)
                else []
            )

        if connection is not None:
            return read(connection)
        with self._connection() as owned:
            return read(owned)

    def _write_provider_records(self, records: list[dict[str, object]], connection) -> None:
        if not records:
            connection.execute("DELETE FROM provider_settings WHERE id = 1")
            return
        connection.execute(
            """INSERT INTO provider_settings(id, provider_configs_json, updated_at) VALUES (1, ?, ?)
            ON CONFLICT(id) DO UPDATE SET provider_configs_json=excluded.provider_configs_json,
            updated_at=excluded.updated_at""",
            (json.dumps(records, ensure_ascii=False), time.time()),
        )

    @staticmethod
    def _assert_unique_provider_name(
        records: list[dict[str, object]], provider_name: str, *, exclude_id: str | None = None
    ) -> None:
        folded = provider_name.casefold()
        for item in records:
            if exclude_id is not None and str(item.get("id")) == exclude_id:
                continue
            if str(item.get("provider_name") or "").casefold() == folded:
                raise ValueError("provider_name already exists")

    def provider_configs(self) -> list[dict[str, object]]:
        return [self._public_provider(item) for item in self._provider_records()]

    def provider_config(self) -> dict[str, object]:
        active = next((item for item in self._provider_records() if item.get("is_active")), None)
        return self._public_provider(active) if active is not None else dict(DEFAULT_PROVIDER_CONFIG)

    def update_provider_config(self, values: Mapping[str, object]) -> dict[str, object]:
        with self._connection(immediate=True) as connection:
            records = self._provider_records(connection)
            active = next((item for item in records if item.get("is_active")), None)
            normalized = normalize_provider_config(active or {}, values)
            ciphertext = str(active.get("api_key_ciphertext") or "") if active else ""
            if isinstance(values.get("api_key"), str) and str(values["api_key"]).strip():
                ciphertext = encrypt_secret(str(values["api_key"]).strip())
            record = {
                **normalized,
                "id": str(active.get("id")) if active else f"provider-{uuid.uuid4().hex}",
                "is_active": True,
                "api_key_ciphertext": ciphertext,
            }
            self._assert_unique_provider_name(records, str(record["provider_name"]), exclude_id=str(record["id"]))
            updated = [record if item.get("id") == record["id"] else {**item, "is_active": False} for item in records]
            if not any(item.get("id") == record["id"] for item in updated):
                updated.append(record)
            self._write_provider_records(updated, connection)
        return self._public_provider(record)

    def add_provider_config(self, values: Mapping[str, object]) -> dict[str, object]:
        normalized = normalize_provider_config({}, values)
        record = {
            **normalized,
            "id": f"provider-{uuid.uuid4().hex}",
            "is_active": False,
            "api_key_ciphertext": encrypt_secret(str(values.get("api_key") or "").strip()),
        }
        with self._connection(immediate=True) as connection:
            records = self._provider_records(connection)
            self._assert_unique_provider_name(records, str(record["provider_name"]))
            records.append(record)
            self._write_provider_records(records, connection)
        return self._public_provider(record)

    def update_provider_config_by_id(self, config_id: str, values: Mapping[str, object]) -> dict[str, object]:
        with self._connection(immediate=True) as connection:
            records = self._provider_records(connection)
            current = next((item for item in records if str(item.get("id")) == config_id), None)
            if current is None:
                raise ValueError("provider configuration not found")
            normalized = normalize_provider_config(current, {**current, **values})
            ciphertext = str(current.get("api_key_ciphertext") or "")
            if isinstance(values.get("api_key"), str) and str(values["api_key"]).strip():
                ciphertext = encrypt_secret(str(values["api_key"]).strip())
            updated = {**current, **normalized, "api_key_ciphertext": ciphertext}
            self._assert_unique_provider_name(records, str(updated["provider_name"]), exclude_id=config_id)
            self._write_provider_records(
                [updated if str(item.get("id")) == config_id else item for item in records], connection
            )
        return self._public_provider(updated)

    def activate_provider_config(self, config_id: str) -> dict[str, object]:
        with self._connection(immediate=True) as connection:
            records = self._provider_records(connection)
            if not any(str(item.get("id")) == config_id for item in records):
                raise ValueError("provider configuration not found")
            records = [{**item, "is_active": str(item.get("id")) == config_id} for item in records]
            self._write_provider_records(records, connection)
        return self._public_provider(next(item for item in records if item.get("is_active")))

    def delete_provider_config(self, config_id: str) -> list[dict[str, object]]:
        with self._connection(immediate=True) as connection:
            records = self._provider_records(connection)
            target = next((item for item in records if str(item.get("id")) == config_id), None)
            if target is None:
                raise ValueError("provider configuration not found")
            if target.get("is_active") and len(records) > 1:
                raise ValueError("activate another provider before deleting the current provider")
            records = [item for item in records if str(item.get("id")) != config_id]
            self._write_provider_records(records, connection)
        return [self._public_provider(item) for item in records]

    def _provider_with_secret(self, config_id: str | None = None) -> dict[str, object] | None:
        records = self._provider_records()
        if config_id:
            return next((item for item in records if str(item.get("id")) == config_id), None)
        return next((item for item in records if item.get("is_active")), None)

    def provider_config_for_discovery(self, config_id: str) -> dict[str, object] | None:
        record = self._provider_with_secret(config_id)
        if record is None:
            return None
        return {
            **record,
            "api_key": decrypt_secret(str(record.get("api_key_ciphertext")))
            if record.get("api_key_ciphertext")
            else "",
        }

    def model_config(self, provider_name: str | None = None):
        from backend.providers import ModelConfig

        records = self._provider_records()
        record = next((item for item in records if item.get("is_active")), None)
        if provider_name:
            folded = provider_name.casefold()
            record = next((item for item in records if str(item.get("provider_name") or "").casefold() == folded), None)
        values = dict(record) if record is not None else dict(DEFAULT_PROVIDER_CONFIG)
        api_key = (
            decrypt_secret(str(record.get("api_key_ciphertext"))) if record and record.get("api_key_ciphertext") else ""
        )
        return ModelConfig.from_mapping({**values, "api_key": api_key, "provider_name": values.get("provider_name")})

    def settings(self) -> dict[str, object]:
        return {
            "profile": self.profile(),
            "agent_config": self.agent_config(),
            "provider_config": self.provider_config(),
            "provider_configs": self.provider_configs(),
            "capability_config": self.capability_config(),
            "runtime_config": normalize_runtime_config(DEFAULT_RUNTIME_CONFIG, self.runtime_config()),
            "sandbox_config": self.sandbox_config(),
            "timezone_options": timezone_options(),
        }


__all__ = ["LocalSettingsStore"]
