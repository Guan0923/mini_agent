"""SQLite-backed user profile, agent, and provider settings."""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Mapping

from backend.configuration import UserConfigStore
from backend.domain import DEFAULT_TIME_ZONE, validate_time_zone
from backend.storage.settings_contract import (
    DEFAULT_AGENT_CONFIG,
    DEFAULT_CAPABILITY_CONFIG,
    DEFAULT_PROFILE,
    DEFAULT_PROVIDER_CONFIG,
    DEFAULT_RUNTIME_CONFIG,
    SUPPORTED_DISPLAY_MODES,
    normalize_agent_config,
    normalize_provider_config,
    normalize_runtime_config,
    timezone_options,
)

from .crypto import _decrypt_secret, _encrypt_secret


class AuthSettingsMixin:
    def _config_store(self, user_id: str) -> UserConfigStore | None:
        store = getattr(self, "config_store", None)
        return store if isinstance(store, UserConfigStore) else None

    def _config(self, user_id: str) -> dict[str, object]:
        store = self._config_store(user_id)
        return store.read() if store is not None else {}

    def cloud_token_for_user(self, user_id: str) -> dict[str, object] | None:
        """Read the locally encrypted cloud token for one account."""

        with self._connection() as connection:
            row = connection.execute(
                "SELECT token_ciphertext, expires_at FROM cloud_credentials WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        if row is None or not row["token_ciphertext"]:
            return None
        return {
            "token": _decrypt_secret(str(row["token_ciphertext"]), user_id),
            "expires_at": float(row["expires_at"] or 0),
        }

    def set_cloud_token(self, user_id: str, token: str, expires_at: float) -> None:
        now = time.time()
        ciphertext = _encrypt_secret(token, user_id)
        with self._connection(immediate=True) as connection:
            connection.execute(
                """INSERT INTO cloud_credentials(user_id, token_ciphertext, expires_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET token_ciphertext=excluded.token_ciphertext,
                    expires_at=excluded.expires_at, updated_at=excluded.updated_at""",
                (user_id, ciphertext, float(expires_at), now),
            )

    def clear_cloud_token(self, user_id: str) -> None:
        with self._connection(immediate=True) as connection:
            connection.execute("DELETE FROM cloud_credentials WHERE user_id = ?", (user_id,))

    def profile_for_user(self, user_id: str) -> dict[str, str]:
        config = self._config(user_id)
        profile = config.get("profile")
        if isinstance(profile, Mapping):
            return {
                "display_name": str(profile.get("display_name") or ""),
                "agent_preferences": str(profile.get("agent_preferences") or ""),
            }
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
        config = self._config(user_id)
        agent = config.get("agent")
        if isinstance(agent, Mapping):
            result = normalize_agent_config(DEFAULT_AGENT_CONFIG, agent)
            return result
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
        config_store = self._config_store(user_id)
        if config_store is not None:
            config_store.update({"agent": result})
            return result
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
        records = self._provider_records_for_user(user_id)
        active = next((item for item in records if item.get("is_active")), None)
        return self._public_provider(active) if active is not None else dict(DEFAULT_PROVIDER_CONFIG)

    @staticmethod
    def _public_provider(record: Mapping[str, object]) -> dict[str, object]:
        return {
            "id": str(record.get("id") or ""),
            "is_active": bool(record.get("is_active")),
            "provider": str(record.get("provider") or "deepseek"),
            "protocol": str(record.get("protocol") or "chat_completions"),
            "base_url": str(record.get("base_url") or ""),
            "model": str(record.get("model") or ""),
            "max_tokens": int(record.get("max_tokens") or 8192),
            "context_size": int(record.get("context_size") or 1024000),
            "tokenizer_model": str(record.get("tokenizer_model") or "deepseek-ai/DeepSeek-V3"),
            "api_key_configured": bool(record.get("api_key_ciphertext")),
        }

    def _provider_records_for_user(self, user_id: str, connection=None) -> list[dict[str, object]]:
        def read_row(active_connection) -> list[dict[str, object]]:
            row = active_connection.execute(
                """SELECT provider, protocol, base_url, model, max_tokens, context_size,
                              tokenizer_model, api_key_ciphertext, provider_configs_json
                       FROM user_provider_settings WHERE user_id = ?""",
                (user_id,),
            ).fetchone()
            if row is None:
                return []
            raw = row["provider_configs_json"]
            try:
                records = json.loads(str(raw or "[]"))
            except (TypeError, ValueError):
                records = []
            if isinstance(records, list) and records:
                return [dict(item) for item in records if isinstance(item, dict) and item.get("id")]
            return []

        if connection is None:
            with self._connection() as owned_connection:
                return read_row(owned_connection)
        return read_row(connection)

    def provider_configs_for_user(self, user_id: str) -> list[dict[str, object]]:
        return [self._public_provider(item) for item in self._provider_records_for_user(user_id)]

    def _provider_with_secret(self, user_id: str, config_id: str | None = None) -> dict[str, object] | None:
        records = self._provider_records_for_user(user_id)
        if config_id:
            return next((item for item in records if str(item.get("id")) == config_id), None)
        return next((item for item in records if item.get("is_active")), None)

    def provider_config_for_discovery(self, user_id: str, config_id: str) -> dict[str, object] | None:
        record = self._provider_with_secret(user_id, config_id)
        if record is None:
            return None
        return {
            **record,
            "api_key": _decrypt_secret(str(record.get("api_key_ciphertext")), user_id)
            if record.get("api_key_ciphertext")
            else "",
        }

    def _write_provider_records(self, user_id: str, records: list[dict[str, object]], connection) -> None:
        active = next((item for item in records if item.get("is_active")), None)
        if active is None and not records:
            connection.execute("DELETE FROM user_provider_settings WHERE user_id = ?", (user_id,))
            return
        now = time.time()
        active = active or {
            "provider": "deepseek",
            "protocol": "chat_completions",
            "base_url": "",
            "model": "",
            "max_tokens": 8192,
            "context_size": 1024000,
            "tokenizer_model": "deepseek-ai/DeepSeek-V3",
            "api_key_ciphertext": "",
        }
        connection.execute(
            """INSERT INTO user_provider_settings
            (user_id, provider, protocol, base_url, model, max_tokens, context_size,
             tokenizer_model, api_key_ciphertext, provider_configs_json, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET provider = excluded.provider,
                protocol = excluded.protocol, base_url = excluded.base_url,
                model = excluded.model, max_tokens = excluded.max_tokens,
                context_size = excluded.context_size, tokenizer_model = excluded.tokenizer_model,
                api_key_ciphertext = excluded.api_key_ciphertext,
                provider_configs_json = excluded.provider_configs_json,
                updated_at = excluded.updated_at""",
            (
                user_id,
                active.get("provider", "deepseek"),
                active.get("protocol", "chat_completions"),
                active.get("base_url", ""),
                active.get("model", ""),
                int(active.get("max_tokens", 8192)),
                int(active.get("context_size", 1024000)),
                active.get("tokenizer_model", "deepseek-ai/DeepSeek-V3"),
                active.get("api_key_ciphertext", ""),
                json.dumps(records, ensure_ascii=False),
                now,
            ),
        )
        config_store = self._config_store(user_id)
        if config_store is not None:
            config_store.update({"providers": {"active_id": str(active.get("id") or "")}})

    def update_provider_config(self, user_id: str, values: Mapping[str, object]) -> dict[str, object]:
        current = self.provider_config_for_user(user_id)
        normalized = normalize_provider_config(current, values)
        with self._connection(immediate=True) as connection:
            records = self._provider_records_for_user(user_id, connection)
            active = next((item for item in records if item.get("is_active")), None)
            ciphertext = str(active.get("api_key_ciphertext") or "") if active else ""
            if isinstance(values.get("api_key"), str) and str(values["api_key"]).strip():
                ciphertext = _encrypt_secret(str(values["api_key"]).strip(), user_id)
            record = {
                **normalized,
                "id": str(active.get("id")) if active else f"provider-{uuid.uuid4().hex}",
                "is_active": True,
                "api_key_ciphertext": ciphertext,
            }
            records = [record if item.get("id") == record["id"] else {**item, "is_active": False} for item in records]
            if not any(item.get("id") == record["id"] for item in records):
                records.append(record)
            self._write_provider_records(user_id, records, connection)
        return self._public_provider(record)

    def add_provider_config(self, user_id: str, values: Mapping[str, object]) -> dict[str, object]:
        normalized = normalize_provider_config({}, values)
        ciphertext = (
            _encrypt_secret(str(values.get("api_key") or "").strip(), user_id)
            if str(values.get("api_key") or "").strip()
            else ""
        )
        record = {
            **normalized,
            "id": f"provider-{uuid.uuid4().hex}",
            "is_active": False,
            "api_key_ciphertext": ciphertext,
        }
        with self._connection(immediate=True) as connection:
            records = self._provider_records_for_user(user_id, connection)
            records.append(record)
            self._write_provider_records(user_id, records, connection)
        return self._public_provider(record)

    def update_provider_config_by_id(
        self, user_id: str, config_id: str, values: Mapping[str, object]
    ) -> dict[str, object]:
        with self._connection(immediate=True) as connection:
            records = self._provider_records_for_user(user_id, connection)
            current = next((item for item in records if str(item.get("id")) == config_id), None)
            if current is None:
                raise ValueError("provider configuration not found")
            normalized = normalize_provider_config(current, {**current, **values})
            key = current.get("api_key_ciphertext", "")
            if isinstance(values.get("api_key"), str) and str(values["api_key"]).strip():
                key = _encrypt_secret(str(values["api_key"]).strip(), user_id)
            updated = {**current, **normalized, "api_key_ciphertext": key}
            self._write_provider_records(
                user_id,
                [updated if str(item.get("id")) == config_id else item for item in records],
                connection,
            )
        return self._public_provider(updated)

    def activate_provider_config(self, user_id: str, config_id: str) -> dict[str, object]:
        with self._connection(immediate=True) as connection:
            records = self._provider_records_for_user(user_id, connection)
            if not any(str(item.get("id")) == config_id for item in records):
                raise ValueError("provider configuration not found")
            records = [{**item, "is_active": str(item.get("id")) == config_id} for item in records]
            self._write_provider_records(user_id, records, connection)
        return self._public_provider(next(item for item in records if item.get("is_active")))

    def delete_provider_config(self, user_id: str, config_id: str) -> list[dict[str, object]]:
        with self._connection(immediate=True) as connection:
            records = self._provider_records_for_user(user_id, connection)
            target = next((item for item in records if str(item.get("id")) == config_id), None)
            if target is None:
                raise ValueError("provider configuration not found")
            if target.get("is_active") and len(records) > 1:
                raise ValueError("activate another provider before deleting the current provider")
            records = [item for item in records if str(item.get("id")) != config_id]
            self._write_provider_records(user_id, records, connection)
        return [self._public_provider(item) for item in records]

    def model_config_for_user(self, user_id: str):
        from backend.providers import ModelConfig

        values = self.provider_config_for_user(user_id)
        record = self._provider_with_secret(user_id)
        api_key = (
            _decrypt_secret(str(record.get("api_key_ciphertext")), user_id)
            if record and record.get("api_key_ciphertext")
            else ""
        )
        return ModelConfig.from_mapping({**values, "api_key": api_key})

    def settings_for_user(self, user_id: str, *, email: str = "") -> dict[str, object]:
        runtime = self.runtime_config_for_user(user_id).get("runtime", {})
        runtime_config = normalize_runtime_config(
            DEFAULT_RUNTIME_CONFIG,
            runtime if isinstance(runtime, Mapping) else {},
        )
        return {
            "profile": {"email": email, **self.profile_for_user(user_id)},
            "agent_config": self.agent_config_for_user(user_id),
            "provider_config": self.provider_config_for_user(user_id),
            "provider_configs": self.provider_configs_for_user(user_id),
            "capability_config": self.capability_config_for_user(user_id),
            "runtime_config": runtime_config,
            "timezone_options": timezone_options(),
        }

    def capability_config_for_user(self, user_id: str) -> dict[str, object]:
        config = self._config(user_id)
        value = config.get("capabilities")
        return dict(value) if isinstance(value, Mapping) else dict(DEFAULT_CAPABILITY_CONFIG)

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
        config = self._config(user_id)
        runtime = config.get("runtime")
        if isinstance(runtime, Mapping):
            return {
                "runtime": {
                    "log_full_messages": bool(runtime.get("log_full_messages", True)),
                    **normalize_runtime_config(DEFAULT_RUNTIME_CONFIG, runtime),
                }
            }
        return {"runtime": {"log_full_messages": True, **DEFAULT_RUNTIME_CONFIG}}

    def update_runtime_config(self, user_id: str, values: Mapping[str, object]) -> dict[str, object]:
        current = self.runtime_config_for_user(user_id).get("runtime", {})
        result = normalize_runtime_config(
            current if isinstance(current, Mapping) else DEFAULT_RUNTIME_CONFIG,
            values,
        )
        config_store = self._config_store(user_id)
        if config_store is None:
            raise ValueError("runtime settings require a local config store")
        current = config_store.read().get("runtime")
        runtime = dict(current) if isinstance(current, Mapping) else {}
        runtime.update(result)
        config_store.update({"runtime": runtime})
        return result

    def device_id_for_user(self, user_id: str) -> str:
        config_store = self._config_store(user_id)
        if config_store is None:
            return f"web_{user_id}"
        config = config_store.ensure_defaults({"sync": {"device_id": f"web_{user_id}"}})
        sync = config.get("sync")
        return str(sync.get("device_id")) if isinstance(sync, Mapping) else f"web_{user_id}"

    def update_profile(self, user_id: str, *, display_name: str, agent_preferences: str) -> dict[str, str]:
        display_name = display_name.strip()
        agent_preferences = agent_preferences.strip()
        if len(display_name) > 80:
            raise ValueError("display_name exceeds 80 characters")
        if len(agent_preferences) > 4000:
            raise ValueError("agent_preferences exceeds 4000 characters")
        config_store = self._config_store(user_id)
        if config_store is not None:
            result = {"display_name": display_name, "agent_preferences": agent_preferences}
            config_store.update({"profile": result})
            return result
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
