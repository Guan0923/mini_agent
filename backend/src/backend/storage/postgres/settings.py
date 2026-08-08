"""PostgreSQL-backed user settings used by the web service."""

from __future__ import annotations

import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from backend.domain import DEFAULT_TIME_ZONE, validate_time_zone
from backend.storage.auth.crypto import _decrypt_secret, _encrypt_secret, _key_material
from backend.storage.settings_contract import (
    DEFAULT_AGENT_CONFIG,
    DEFAULT_CAPABILITY_CONFIG,
    DEFAULT_PROFILE,
    DEFAULT_PROVIDER_CONFIG,
    SUPPORTED_DISPLAY_MODES,
    normalize_agent_config,
    normalize_provider_config,
    timezone_options,
)

# Historical private names remain available to integrations that imported the
# old ``backend.api.settings_store`` module directly.
_encrypt = _encrypt_secret
_decrypt = _decrypt_secret
_key = _key_material


class PostgresSettingsRepository:
    def __init__(self, database_url: str) -> None:
        self.database_url = database_url
        self.initialize()

    def _connect(self):
        try:
            import psycopg
        except ImportError as exc:
            raise RuntimeError("psycopg is required for DATABASE_URL") from exc
        return psycopg.connect(self.database_url, connect_timeout=10)

    def initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS user_profiles (
                    user_id TEXT PRIMARY KEY, display_name TEXT NOT NULL DEFAULT '',
                    agent_preferences TEXT NOT NULL DEFAULT '', updated_at DOUBLE PRECISION NOT NULL)"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS user_agent_settings (
                    user_id TEXT PRIMARY KEY, tone TEXT NOT NULL DEFAULT 'balanced',
                    verbosity TEXT NOT NULL DEFAULT 'balanced', initiative TEXT NOT NULL DEFAULT 'balanced',
                    custom_instructions TEXT NOT NULL DEFAULT '', display_mode TEXT NOT NULL DEFAULT 'medium',
                    timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai', location_enabled BOOLEAN NOT NULL DEFAULT FALSE,
                    updated_at DOUBLE PRECISION NOT NULL)"""
            )
            connection.execute(
                "ALTER TABLE user_agent_settings ADD COLUMN IF NOT EXISTS display_mode TEXT NOT NULL DEFAULT 'medium'"
            )
            connection.execute(
                "ALTER TABLE user_agent_settings ADD COLUMN IF NOT EXISTS timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai'"
            )
            connection.execute(
                "ALTER TABLE user_agent_settings ADD COLUMN IF NOT EXISTS location_enabled BOOLEAN NOT NULL DEFAULT FALSE"
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS user_provider_settings (
                    user_id TEXT PRIMARY KEY, provider TEXT NOT NULL DEFAULT 'deepseek',
                    protocol TEXT NOT NULL DEFAULT 'chat_completions', base_url TEXT NOT NULL DEFAULT '',
                    model TEXT NOT NULL DEFAULT '', max_tokens INTEGER NOT NULL DEFAULT 8192,
                    context_size INTEGER NOT NULL DEFAULT 1024000,
                    tokenizer_model TEXT NOT NULL DEFAULT 'deepseek-ai/DeepSeek-V3',
                    api_key_ciphertext TEXT NOT NULL DEFAULT '', updated_at DOUBLE PRECISION NOT NULL)"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS user_capability_settings (
                    user_id TEXT PRIMARY KEY, settings_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                    updated_at DOUBLE PRECISION NOT NULL)"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS server_defaults (
                    name TEXT PRIMARY KEY, settings_json JSONB NOT NULL,
                    updated_at DOUBLE PRECISION NOT NULL)"""
            )

    def profile_for_user(self, user_id: str) -> dict[str, str]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT display_name, agent_preferences FROM user_profiles WHERE user_id=%s", (user_id,)
            ).fetchone()
        return (
            dict(DEFAULT_PROFILE)
            if row is None
            else {"display_name": str(row[0] or ""), "agent_preferences": str(row[1] or "")}
        )

    def update_profile(self, user_id: str, *, display_name: str, agent_preferences: str) -> dict[str, str]:
        display_name = str(display_name or "").strip()
        agent_preferences = str(agent_preferences or "").strip()
        if len(display_name) > 80 or len(agent_preferences) > 4000:
            raise ValueError("profile field exceeds its length limit")
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO user_profiles(user_id,display_name,agent_preferences,updated_at)
                   VALUES (%s,%s,%s,%s) ON CONFLICT(user_id) DO UPDATE SET
                   display_name=EXCLUDED.display_name,agent_preferences=EXCLUDED.agent_preferences,
                   updated_at=EXCLUDED.updated_at""",
                (user_id, display_name, agent_preferences, time.time()),
            )
        return {"display_name": display_name, "agent_preferences": agent_preferences}

    def agent_config_for_user(self, user_id: str) -> dict[str, object]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT tone,verbosity,initiative,custom_instructions,display_mode,timezone,location_enabled "
                "FROM user_agent_settings WHERE user_id=%s",
                (user_id,),
            ).fetchone()
        if row is None:
            return dict(DEFAULT_AGENT_CONFIG)
        display_mode = str(row[4] or "medium")
        if display_mode not in SUPPORTED_DISPLAY_MODES:
            display_mode = "medium"
        timezone = str(row[5] or DEFAULT_TIME_ZONE)
        try:
            timezone = validate_time_zone(timezone)
        except ValueError:
            timezone = DEFAULT_TIME_ZONE
        return {
            "tone": str(row[0] or "balanced"),
            "verbosity": str(row[1] or "balanced"),
            "initiative": str(row[2] or "balanced"),
            "custom_instructions": str(row[3] or ""),
            "display_mode": display_mode,
            "timezone": timezone,
            "location_enabled": bool(row[6]),
        }

    def update_agent_config(self, user_id: str, values: Mapping[str, object]) -> dict[str, object]:
        current = self.agent_config_for_user(user_id)
        result = normalize_agent_config(current, values)
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO user_agent_settings
                   (user_id,tone,verbosity,initiative,custom_instructions,display_mode,timezone,location_enabled,updated_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(user_id) DO UPDATE SET
                   tone=EXCLUDED.tone,verbosity=EXCLUDED.verbosity,initiative=EXCLUDED.initiative,
                   custom_instructions=EXCLUDED.custom_instructions,display_mode=EXCLUDED.display_mode,
                   timezone=EXCLUDED.timezone,location_enabled=EXCLUDED.location_enabled,updated_at=EXCLUDED.updated_at""",
                (
                    user_id,
                    result["tone"],
                    result["verbosity"],
                    result["initiative"],
                    result["custom_instructions"],
                    result["display_mode"],
                    result["timezone"],
                    bool(result["location_enabled"]),
                    time.time(),
                ),
            )
        return result

    def provider_config_for_user(self, user_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT provider,protocol,base_url,model,max_tokens,context_size,tokenizer_model,
                          api_key_ciphertext FROM user_provider_settings WHERE user_id=%s""",
                (user_id,),
            ).fetchone()
        if row is None:
            return dict(DEFAULT_PROVIDER_CONFIG)
        return {
            "provider": str(row[0] or "deepseek"),
            "protocol": str(row[1] or "chat_completions"),
            "base_url": str(row[2] or ""),
            "model": str(row[3] or ""),
            "max_tokens": int(row[4] or 8192),
            "context_size": int(row[5] or 1024000),
            "tokenizer_model": str(row[6] or "deepseek-ai/DeepSeek-V3"),
            "api_key_configured": bool(row[7]),
        }

    def update_provider_config(self, user_id: str, values: Mapping[str, object]) -> dict[str, Any]:
        current = self.provider_config_for_user(user_id)
        normalized = normalize_provider_config(current, values)
        provider = str(normalized["provider"])
        protocol = str(normalized["protocol"])
        base_url = str(normalized["base_url"])
        model = str(normalized["model"])
        max_tokens = int(normalized["max_tokens"])
        context_size = int(normalized["context_size"])
        tokenizer = str(normalized["tokenizer_model"])
        with self._connect() as connection:
            row = connection.execute(
                "SELECT api_key_ciphertext FROM user_provider_settings WHERE user_id=%s", (user_id,)
            ).fetchone()
            ciphertext = str(row[0] or "") if row else ""
            if isinstance(values.get("api_key"), str) and str(values["api_key"]).strip():
                ciphertext = _encrypt_secret(str(values["api_key"]).strip())
            connection.execute(
                """INSERT INTO user_provider_settings(user_id,provider,protocol,base_url,model,max_tokens,
                   context_size,tokenizer_model,api_key_ciphertext,updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT(user_id) DO UPDATE SET provider=EXCLUDED.provider,protocol=EXCLUDED.protocol,
                   base_url=EXCLUDED.base_url,model=EXCLUDED.model,max_tokens=EXCLUDED.max_tokens,
                   context_size=EXCLUDED.context_size,tokenizer_model=EXCLUDED.tokenizer_model,
                   api_key_ciphertext=EXCLUDED.api_key_ciphertext,updated_at=EXCLUDED.updated_at""",
                (
                    user_id,
                    provider,
                    protocol,
                    base_url,
                    model,
                    max_tokens,
                    context_size,
                    tokenizer,
                    ciphertext,
                    time.time(),
                ),
            )
        return {
            "provider": provider,
            "protocol": protocol,
            "base_url": base_url,
            "model": model,
            "max_tokens": max_tokens,
            "context_size": context_size,
            "tokenizer_model": tokenizer,
            "api_key_configured": bool(ciphertext),
        }

    def import_legacy_provider_config(self, user_id: str, config_path: Path) -> bool:
        """Import the legacy TOML model section once into PostgreSQL."""
        current = self.provider_config_for_user(user_id)
        if current.get("base_url") and current.get("model"):
            return False
        if not config_path.exists():
            return False
        try:
            from backend.configuration import load_config, section

            values = dict(section(load_config(config_path), "model"))
        except (OSError, ValueError, KeyError):
            return False
        if not values.get("base_url") or not values.get("model") or not values.get("api_key"):
            return False
        self.update_provider_config(user_id, values)
        return True

    def settings_for_user(self, user_id: str, *, email: str = "") -> dict[str, Any]:
        return {
            "profile": {"email": email, **self.profile_for_user(user_id)},
            "agent_config": self.agent_config_for_user(user_id),
            "provider_config": self.provider_config_for_user(user_id),
            "capability_config": dict(DEFAULT_CAPABILITY_CONFIG),
            "timezone_options": timezone_options(),
        }

    def agent_preferences_for_user(self, user_id: str) -> str:
        agent = self.agent_config_for_user(user_id)
        legacy = self.profile_for_user(user_id).get("agent_preferences", "")
        return "\n".join(
            item
            for item in (
                "" if agent["tone"] == "balanced" else f"Preferred tone: {agent['tone']}",
                "" if agent["verbosity"] == "balanced" else f"Preferred verbosity: {agent['verbosity']}",
                "" if agent["initiative"] == "balanced" else f"Preferred initiative: {agent['initiative']}",
                agent["custom_instructions"],
                legacy,
            )
            if item
        ).strip()

    def runtime_config_for_user(self, user_id: str) -> dict[str, object]:
        del user_id
        return {"runtime": {"log_full_messages": True}}

    def model_config_for_user(self, user_id: str):
        from backend.providers import ModelConfig

        values = self.provider_config_for_user(user_id)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT api_key_ciphertext FROM user_provider_settings WHERE user_id=%s", (user_id,)
            ).fetchone()
        return ModelConfig.from_mapping({**values, "api_key": _decrypt_secret(str(row[0])) if row and row[0] else ""})
