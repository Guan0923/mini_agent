"""PostgreSQL-backed user settings used by the web service."""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .auth_store import (
    DEFAULT_AGENT_CONFIG,
    DEFAULT_CAPABILITY_CONFIG,
    DEFAULT_PROFILE,
    DEFAULT_PROVIDER_CONFIG,
)


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
                    custom_instructions TEXT NOT NULL DEFAULT '', updated_at DOUBLE PRECISION NOT NULL)"""
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
        return dict(DEFAULT_PROFILE) if row is None else {
            "display_name": str(row[0] or ""), "agent_preferences": str(row[1] or "")
        }

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

    def agent_config_for_user(self, user_id: str) -> dict[str, str]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT tone,verbosity,initiative,custom_instructions FROM user_agent_settings WHERE user_id=%s",
                (user_id,),
            ).fetchone()
        if row is None:
            return dict(DEFAULT_AGENT_CONFIG)
        return {"tone": str(row[0] or "balanced"), "verbosity": str(row[1] or "balanced"),
                "initiative": str(row[2] or "balanced"), "custom_instructions": str(row[3] or "")}

    def update_agent_config(self, user_id: str, values: Mapping[str, object]) -> dict[str, str]:
        result = dict(DEFAULT_AGENT_CONFIG)
        for key in result:
            if key in values:
                result[key] = str(values[key] or "").strip()
                if len(result[key]) > (4000 if key == "custom_instructions" else 40):
                    raise ValueError(f"{key} exceeds its length limit")
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO user_agent_settings(user_id,tone,verbosity,initiative,custom_instructions,updated_at)
                   VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT(user_id) DO UPDATE SET
                   tone=EXCLUDED.tone,verbosity=EXCLUDED.verbosity,initiative=EXCLUDED.initiative,
                   custom_instructions=EXCLUDED.custom_instructions,updated_at=EXCLUDED.updated_at""",
                (user_id, result["tone"], result["verbosity"], result["initiative"],
                 result["custom_instructions"], time.time()),
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
        return {"provider": str(row[0] or "deepseek"), "protocol": str(row[1] or "chat_completions"),
                "base_url": str(row[2] or ""), "model": str(row[3] or ""), "max_tokens": int(row[4] or 8192),
                "context_size": int(row[5] or 1024000), "tokenizer_model": str(row[6] or "deepseek-ai/DeepSeek-V3"),
                "api_key_configured": bool(row[7])}

    def update_provider_config(self, user_id: str, values: Mapping[str, object]) -> dict[str, Any]:
        current = self.provider_config_for_user(user_id)
        protocol = str(values.get("protocol", current.get("protocol", "chat_completions")) or "").strip().lower()
        if protocol not in {"chat_completions", "responses", "messages"}:
            raise ValueError("protocol must be chat_completions, responses, or messages")
        base_url = str(values.get("base_url", current.get("base_url", "")) or "").strip()
        model = str(values.get("model", current.get("model", "")) or "").strip()
        if not base_url or not model:
            raise ValueError("base_url and model are required")
        max_tokens = int(values.get("max_tokens", current.get("max_tokens", 8192)))
        context_size = int(values.get("context_size", current.get("context_size", 1024000)))
        if not 1 <= max_tokens <= 384000 or context_size <= max_tokens:
            raise ValueError("invalid token limits")
        provider = str(values.get("provider", current.get("provider", "deepseek")) or "deepseek").strip().lower()
        tokenizer = str(values.get("tokenizer_model", current.get("tokenizer_model", "deepseek-ai/DeepSeek-V3")) or "").strip()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT api_key_ciphertext FROM user_provider_settings WHERE user_id=%s", (user_id,)
            ).fetchone()
            ciphertext = str(row[0] or "") if row else ""
            if isinstance(values.get("api_key"), str) and str(values["api_key"]).strip():
                ciphertext = _encrypt(str(values["api_key"]).strip())
            connection.execute(
                """INSERT INTO user_provider_settings(user_id,provider,protocol,base_url,model,max_tokens,
                   context_size,tokenizer_model,api_key_ciphertext,updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT(user_id) DO UPDATE SET provider=EXCLUDED.provider,protocol=EXCLUDED.protocol,
                   base_url=EXCLUDED.base_url,model=EXCLUDED.model,max_tokens=EXCLUDED.max_tokens,
                   context_size=EXCLUDED.context_size,tokenizer_model=EXCLUDED.tokenizer_model,
                   api_key_ciphertext=EXCLUDED.api_key_ciphertext,updated_at=EXCLUDED.updated_at""",
                (user_id, provider, protocol, base_url, model, max_tokens, context_size, tokenizer, ciphertext, time.time()),
            )
        return {"provider": provider, "protocol": protocol, "base_url": base_url, "model": model,
                "max_tokens": max_tokens, "context_size": context_size, "tokenizer_model": tokenizer,
                "api_key_configured": bool(ciphertext)}

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
        return {"profile": {"email": email, **self.profile_for_user(user_id)},
                "agent_config": self.agent_config_for_user(user_id),
                "provider_config": self.provider_config_for_user(user_id),
                "capability_config": dict(DEFAULT_CAPABILITY_CONFIG)}

    def agent_preferences_for_user(self, user_id: str) -> str:
        agent = self.agent_config_for_user(user_id)
        legacy = self.profile_for_user(user_id).get("agent_preferences", "")
        return "\n".join(item for item in (
            "" if agent["tone"] == "balanced" else f"Preferred tone: {agent['tone']}",
            "" if agent["verbosity"] == "balanced" else f"Preferred verbosity: {agent['verbosity']}",
            "" if agent["initiative"] == "balanced" else f"Preferred initiative: {agent['initiative']}",
            agent["custom_instructions"], legacy,
        ) if item).strip()

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
        return ModelConfig.from_mapping({**values, "api_key": _decrypt(str(row[0])) if row and row[0] else ""})


def _key() -> bytes:
    value = os.environ.get("MINI_AGENT_SECRET_KEY", "")
    return hashlib.sha256((value or str(Path.home())).encode("utf-8")).digest()


def _encrypt(value: str) -> str:
    raw = value.encode("utf-8")
    key = _key()
    return base64.urlsafe_b64encode(secrets.token_bytes(16) + bytes(v ^ key[i % len(key)] for i, v in enumerate(raw))).decode("ascii")


def _decrypt(value: str) -> str:
    try:
        raw = base64.urlsafe_b64decode(value)
    except (ValueError, UnicodeError):
        return ""
    key = _key()
    return bytes(v ^ key[i % len(key)] for i, v in enumerate(raw[16:])).decode("utf-8", errors="replace")