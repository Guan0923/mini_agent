"""SQLite-backed user profile, agent, and provider settings."""

from __future__ import annotations

import time
from collections.abc import Mapping
from pathlib import Path

from backend.domain import DEFAULT_TIME_ZONE, validate_time_zone
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

from .crypto import _decrypt_secret, _encrypt_secret


class AuthSettingsMixin:
    def profile_for_user(self, user_id: str) -> dict[str, str]:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT display_name, agent_preferences FROM user_profiles WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        if row is None:
            return dict(DEFAULT_PROFILE)
        return {
            "display_name": str(row["display_name"] or ""),
            "agent_preferences": str(row["agent_preferences"] or ""),
        }

    def agent_config_for_user(self, user_id: str) -> dict[str, object]:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT tone, verbosity, initiative, custom_instructions, display_mode, timezone, location_enabled "
                "FROM user_agent_settings WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        if row is None:
            return dict(DEFAULT_AGENT_CONFIG)
        display_mode = str(row["display_mode"] or DEFAULT_AGENT_CONFIG["display_mode"])
        if display_mode not in SUPPORTED_DISPLAY_MODES:
            display_mode = str(DEFAULT_AGENT_CONFIG["display_mode"])
        timezone = str(row["timezone"] or DEFAULT_TIME_ZONE)
        try:
            timezone = validate_time_zone(timezone)
        except ValueError:
            timezone = DEFAULT_TIME_ZONE
        return {
            "tone": str(row["tone"] or "balanced"),
            "verbosity": str(row["verbosity"] or "balanced"),
            "initiative": str(row["initiative"] or "balanced"),
            "custom_instructions": str(row["custom_instructions"] or ""),
            "display_mode": display_mode,
            "timezone": timezone,
            "location_enabled": bool(row["location_enabled"]),
        }

    def update_agent_config(self, user_id: str, values: Mapping[str, object]) -> dict[str, object]:
        current = self.agent_config_for_user(user_id)
        result = normalize_agent_config(current, values)
        with self._connection(immediate=True) as connection:
            connection.execute(
                """INSERT INTO user_agent_settings
                (user_id, tone, verbosity, initiative, custom_instructions, display_mode, timezone, location_enabled, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET tone = excluded.tone,
                    verbosity = excluded.verbosity, initiative = excluded.initiative,
                    custom_instructions = excluded.custom_instructions,
                    display_mode = excluded.display_mode, timezone = excluded.timezone,
                    location_enabled = excluded.location_enabled, updated_at = excluded.updated_at""",
                (
                    user_id,
                    result["tone"],
                    result["verbosity"],
                    result["initiative"],
                    result["custom_instructions"],
                    result["display_mode"],
                    result["timezone"],
                    int(bool(result["location_enabled"])),
                    time.time(),
                ),
            )
        return result

    def provider_config_for_user(self, user_id: str) -> dict[str, object]:
        with self._connection() as connection:
            row = connection.execute(
                """SELECT provider, protocol, base_url, model, max_tokens, context_size,
                          tokenizer_model, api_key_ciphertext
                   FROM user_provider_settings WHERE user_id = ?""",
                (user_id,),
            ).fetchone()
        if row is None:
            return dict(DEFAULT_PROVIDER_CONFIG)
        return {
            "provider": str(row["provider"] or "deepseek"),
            "protocol": str(row["protocol"] or "chat_completions"),
            "base_url": str(row["base_url"] or ""),
            "model": str(row["model"] or ""),
            "max_tokens": int(row["max_tokens"] or 8192),
            "context_size": int(row["context_size"] or 1024000),
            "tokenizer_model": str(row["tokenizer_model"] or "deepseek-ai/DeepSeek-V3"),
            "api_key_configured": bool(row["api_key_ciphertext"]),
        }

    def update_provider_config(self, user_id: str, values: Mapping[str, object]) -> dict[str, object]:
        current = self.provider_config_for_user(user_id)
        normalized = normalize_provider_config(current, values)
        provider = str(normalized["provider"])
        protocol = str(normalized["protocol"])
        base_url = str(normalized["base_url"])
        model = str(normalized["model"])
        max_tokens = int(normalized["max_tokens"])
        context_size = int(normalized["context_size"])
        tokenizer_model = str(normalized["tokenizer_model"])
        key_value = values.get("api_key")
        with self._connection(immediate=True) as connection:
            existing = connection.execute(
                "SELECT api_key_ciphertext FROM user_provider_settings WHERE user_id = ?", (user_id,)
            ).fetchone()
            ciphertext = str(existing["api_key_ciphertext"] or "") if existing else ""
            if isinstance(key_value, str) and key_value.strip():
                ciphertext = _encrypt_secret(key_value.strip())
            connection.execute(
                """INSERT INTO user_provider_settings
                (user_id, provider, protocol, base_url, model, max_tokens, context_size,
                 tokenizer_model, api_key_ciphertext, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET provider = excluded.provider,
                    protocol = excluded.protocol, base_url = excluded.base_url,
                    model = excluded.model, max_tokens = excluded.max_tokens,
                    context_size = excluded.context_size, tokenizer_model = excluded.tokenizer_model,
                    api_key_ciphertext = excluded.api_key_ciphertext,
                    updated_at = excluded.updated_at""",
                (
                    user_id,
                    provider,
                    protocol,
                    base_url,
                    model,
                    max_tokens,
                    context_size,
                    tokenizer_model,
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
            "tokenizer_model": tokenizer_model,
            "api_key_configured": bool(ciphertext),
        }

    def import_legacy_provider_config(self, user_id: str, config_path: Path) -> bool:
        """Import the old TOML model table once without copying it to a user root."""
        with self._connection() as connection:
            exists = connection.execute(
                "SELECT 1 FROM user_provider_settings WHERE user_id = ? AND base_url <> '' AND model <> ''",
                (user_id,),
            ).fetchone()
        if exists or not config_path.exists():
            return False
        try:
            from backend.configuration import load_config, section

            values = dict(section(load_config(config_path), "model"))
            if not values.get("base_url") or not values.get("model") or not values.get("api_key"):
                return False
            self.update_provider_config(user_id, values)
            self.set_metadata(f"provider_migration:{user_id}", "complete")
            return True
        except (OSError, ValueError, KeyError):
            return False

    def model_config_for_user(self, user_id: str):
        from backend.providers import ModelConfig

        values = self.provider_config_for_user(user_id)
        with self._connection() as connection:
            row = connection.execute(
                "SELECT api_key_ciphertext FROM user_provider_settings WHERE user_id = ?", (user_id,)
            ).fetchone()
        api_key = _decrypt_secret(str(row["api_key_ciphertext"])) if row and row["api_key_ciphertext"] else ""
        return ModelConfig.from_mapping({**values, "api_key": api_key})

    def settings_for_user(self, user_id: str, *, email: str = "") -> dict[str, object]:
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
        parts = [
            f"Preferred tone: {agent['tone']}" if agent["tone"] != "balanced" else "",
            f"Preferred verbosity: {agent['verbosity']}" if agent["verbosity"] != "balanced" else "",
            f"Preferred initiative: {agent['initiative']}" if agent["initiative"] != "balanced" else "",
            agent["custom_instructions"],
            legacy,
        ]
        return "\n".join(item for item in parts if item).strip()

    def runtime_config_for_user(self, user_id: str) -> dict[str, object]:
        del user_id
        return {"runtime": {"log_full_messages": True}}

    def device_id_for_user(self, user_id: str) -> str:
        return f"web_{user_id}"

    def update_profile(self, user_id: str, *, display_name: str, agent_preferences: str) -> dict[str, str]:
        display_name = display_name.strip()
        agent_preferences = agent_preferences.strip()
        if len(display_name) > 80:
            raise ValueError("display_name exceeds 80 characters")
        if len(agent_preferences) > 4000:
            raise ValueError("agent_preferences exceeds 4000 characters")
        now = time.time()
        with self._connection(immediate=True) as connection:
            connection.execute(
                """INSERT INTO user_profiles(user_id, display_name, agent_preferences, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    display_name = excluded.display_name,
                    agent_preferences = excluded.agent_preferences,
                    updated_at = excluded.updated_at""",
                (user_id, display_name, agent_preferences, now),
            )
        return {"display_name": display_name, "agent_preferences": agent_preferences}
