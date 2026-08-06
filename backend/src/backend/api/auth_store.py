"""Durable, secret-safe authentication repository backed by SQLite."""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
import sqlite3
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from pwdlib import PasswordHash

from .auth_schema import SCHEMA
from .auth_types import UserIdentity

DEFAULT_PROFILE: dict[str, str] = {"display_name": "", "agent_preferences": ""}
DEFAULT_AGENT_CONFIG: dict[str, str] = {
    "tone": "balanced",
    "verbosity": "balanced",
    "initiative": "balanced",
    "custom_instructions": "",
}
DEFAULT_PROVIDER_CONFIG: dict[str, object] = {
    "provider": "deepseek",
    "protocol": "chat_completions",
    "base_url": "",
    "model": "",
    "max_tokens": 8192,
    "context_size": 1024000,
    "tokenizer_model": "deepseek-ai/DeepSeek-V3",
    "api_key_configured": False,
}
DEFAULT_CAPABILITY_CONFIG: dict[str, object] = {}


class AuthStore:
    """Own all authentication mutations and never expose raw secrets to storage."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.passwords = PasswordHash.recommended()
        with self._connection() as connection:
            connection.executescript(SCHEMA)

    @contextmanager
    def _connection(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
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

    @staticmethod
    def _token_hash(value: str) -> str:
        return hashlib.sha256(value.encode()).hexdigest()

    @staticmethod
    def new_secret() -> str:
        return secrets.token_urlsafe(48)

    def user_by_email(self, email: str) -> UserIdentity | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT id, email, legacy_owner FROM users WHERE email = ?",
                (email,),
            ).fetchone()
        return self._identity(row) if row else None

    def user_by_id(self, user_id: str) -> UserIdentity | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT id, email, legacy_owner FROM users WHERE id = ?",
                (user_id,),
            ).fetchone()
        return self._identity(row) if row else None

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

    def agent_config_for_user(self, user_id: str) -> dict[str, str]:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT tone, verbosity, initiative, custom_instructions "
                "FROM user_agent_settings WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        if row is None:
            return dict(DEFAULT_AGENT_CONFIG)
        return {
            "tone": str(row["tone"] or "balanced"),
            "verbosity": str(row["verbosity"] or "balanced"),
            "initiative": str(row["initiative"] or "balanced"),
            "custom_instructions": str(row["custom_instructions"] or ""),
        }

    def update_agent_config(self, user_id: str, values: Mapping[str, object]) -> dict[str, str]:
        result = dict(DEFAULT_AGENT_CONFIG)
        for key in result:
            if key in values:
                value = str(values[key] or "").strip()
                limit = 4000 if key == "custom_instructions" else 40
                if len(value) > limit:
                    raise ValueError(f"{key} exceeds {limit} characters")
                result[key] = value
        with self._connection(immediate=True) as connection:
            connection.execute(
                """INSERT INTO user_agent_settings
                (user_id, tone, verbosity, initiative, custom_instructions, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET tone = excluded.tone,
                    verbosity = excluded.verbosity, initiative = excluded.initiative,
                    custom_instructions = excluded.custom_instructions,
                    updated_at = excluded.updated_at""",
                (user_id, result["tone"], result["verbosity"], result["initiative"],
                 result["custom_instructions"], time.time()),
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
        protocol = str(values.get("protocol", current.get("protocol", "chat_completions")) or "").strip().lower()
        if protocol not in {"chat_completions", "responses", "messages"}:
            raise ValueError("protocol must be chat_completions, responses, or messages")
        provider = str(values.get("provider", current.get("provider", "deepseek")) or "deepseek").strip().lower()
        base_url = str(values.get("base_url", current.get("base_url", "")) or "").strip()
        model = str(values.get("model", current.get("model", "")) or "").strip()
        tokenizer_model = str(
            values.get("tokenizer_model", current.get("tokenizer_model", "deepseek-ai/DeepSeek-V3"))
            or ""
        ).strip()
        if len(base_url) > 2000 or len(model) > 300 or len(tokenizer_model) > 300:
            raise ValueError("provider fields exceed their length limits")
        try:
            max_tokens = int(values.get("max_tokens", current.get("max_tokens", 8192)))
            context_size = int(values.get("context_size", current.get("context_size", 1024000)))
        except (TypeError, ValueError) as exc:
            raise ValueError("token limits must be integers") from exc
        if not 1 <= max_tokens <= 384000 or context_size <= max_tokens:
            raise ValueError("invalid token limits")
        if not base_url or not model:
            raise ValueError("base_url and model are required")
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
                (user_id, provider, protocol, base_url, model, max_tokens, context_size,
                 tokenizer_model, ciphertext, time.time()),
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

    @staticmethod
    def _identity(row: sqlite3.Row) -> UserIdentity:
        return UserIdentity(str(row["id"]), str(row["email"]), bool(row["legacy_owner"]))

    def password_hash(self, password: str) -> str:
        return self.passwords.hash(password)

    def verify_password(self, password: str, password_hash: str) -> bool:
        try:
            return self.passwords.verify(password, password_hash)
        except (ValueError, TypeError):
            return False

    def password_hash_for_user(self, email: str) -> str | None:
        with self._connection() as connection:
            row = connection.execute("SELECT password_hash FROM users WHERE email = ?", (email,)).fetchone()
        return str(row[0]) if row else None

    def insert_challenge(
        self,
        email: str,
        purpose: str,
        code: str,
        ip_address: str | None,
        *,
        ttl_seconds: int = 600,
    ) -> None:
        now = time.time()
        with self._connection(immediate=True) as connection:
            connection.execute(
                "UPDATE verification_challenges SET consumed_at = ? "
                "WHERE email = ? AND purpose = ? AND consumed_at IS NULL",
                (now, email, purpose),
            )
            connection.execute(
                """INSERT INTO verification_challenges
                (id, email, purpose, code_hash, ip_address, created_at, expires_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    uuid4().hex,
                    email,
                    purpose,
                    self.password_hash(code),
                    ip_address,
                    now,
                    now + ttl_seconds,
                ),
            )

    def can_send(self, email: str, purpose: str, ip_address: str | None) -> tuple[bool, int]:
        """Return whether a code may be sent and a retry-after estimate."""
        now = time.time()
        window_seconds = 3600
        window_start = int(now) - (int(now) % window_seconds)
        with self._connection(immediate=True) as connection:
            latest = connection.execute(
                """SELECT created_at FROM verification_challenges
                WHERE email = ? AND purpose = ? ORDER BY created_at DESC LIMIT 1""",
                (email, purpose),
            ).fetchone()
            if latest:
                remaining = max(0, 60 - int(now - float(latest[0])))
                if remaining:
                    return False, remaining

            keys = [(f"email:{email}", 5)]
            if ip_address:
                keys.append((f"ip:{ip_address}", 20))
            action = f"code:{purpose}:hour"
            counts: list[tuple[str, int]] = []
            for key, limit in keys:
                row = connection.execute(
                    "SELECT count FROM rate_limits WHERE key = ? AND action = ? AND window_start = ?",
                    (key, action, window_start),
                ).fetchone()
                count = int(row[0]) if row else 0
                if count >= limit:
                    return False, window_seconds
                counts.append((key, count))
            for key, count in counts:
                if count:
                    connection.execute(
                        "UPDATE rate_limits SET count = ? WHERE key = ? AND action = ? AND window_start = ?",
                        (count + 1, key, action, window_start),
                    )
                else:
                    connection.execute(
                        "INSERT INTO rate_limits(key, action, window_start, count) VALUES (?, ?, ?, 1)",
                        (key, action, window_start),
                    )
        return True, 0

    def consume_limit(self, key: str, action: str, limit: int, window_seconds: int) -> bool:
        now = int(time.time())
        window_start = now - (now % window_seconds)
        with self._connection(immediate=True) as connection:
            row = connection.execute(
                "SELECT count FROM rate_limits WHERE key = ? AND action = ? AND window_start = ?",
                (key, action, window_start),
            ).fetchone()
            count = int(row[0]) if row else 0
            if count >= limit:
                return False
            if row:
                connection.execute(
                    "UPDATE rate_limits SET count = ? WHERE key = ? AND action = ? AND window_start = ?",
                    (count + 1, key, action, window_start),
                )
            else:
                connection.execute(
                    "INSERT INTO rate_limits(key, action, window_start, count) VALUES (?, ?, ?, 1)",
                    (key, action, window_start),
                )
        return True

    def _consume_challenge(self, connection: sqlite3.Connection, email: str, purpose: str, code: str) -> bool:
        row = connection.execute(
            """SELECT id, code_hash, expires_at, attempts FROM verification_challenges
            WHERE email = ? AND purpose = ? AND consumed_at IS NULL
            ORDER BY created_at DESC LIMIT 1""",
            (email, purpose),
        ).fetchone()
        if row is None:
            return False
        now = time.time()
        if float(row["expires_at"]) <= now or int(row["attempts"]) >= 5:
            connection.execute("UPDATE verification_challenges SET consumed_at = ? WHERE id = ?", (now, row["id"]))
            return False
        if not self.verify_password(code, str(row["code_hash"])):
            attempts = int(row["attempts"]) + 1
            connection.execute(
                "UPDATE verification_challenges SET attempts = ?, consumed_at = ? WHERE id = ?",
                (attempts, now if attempts >= 5 else None, row["id"]),
            )
            return False
        connection.execute("UPDATE verification_challenges SET consumed_at = ? WHERE id = ?", (now, row["id"]))
        return True

    def register_user(self, email: str, code: str, password: str) -> UserIdentity:
        password_hash = self.password_hash(password)
        now = time.time()
        with self._connection(immediate=True) as connection:
            if not self._consume_challenge(connection, email, "register", code):
                raise ValueError("验证码无效或已过期。")
            existing = connection.execute("SELECT 1 FROM users WHERE email = ?", (email,)).fetchone()
            if existing:
                raise ValueError("该邮箱已注册，请直接登录。")
            user_id = uuid4().hex
            first = connection.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
            connection.execute(
                "INSERT INTO users(id, email, password_hash, created_at, legacy_owner) VALUES (?, ?, ?, ?, ?)",
                (user_id, email, password_hash, now, int(first)),
            )
        return UserIdentity(user_id, email, first)

    def authenticate(self, email: str, password: str) -> UserIdentity | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT id, email, password_hash, legacy_owner FROM users WHERE email = ?",
                (email,),
            ).fetchone()
        if row is None or not self.verify_password(password, str(row["password_hash"])):
            return None
        return self._identity(row)

    def reset_password(self, email: str, code: str, password: str) -> UserIdentity:
        password_hash = self.password_hash(password)
        with self._connection(immediate=True) as connection:
            if not self._consume_challenge(connection, email, "reset", code):
                raise ValueError("验证码无效或已过期。")
            row = connection.execute(
                "SELECT id, email, legacy_owner FROM users WHERE email = ?",
                (email,),
            ).fetchone()
            if row is None:
                raise ValueError("验证码无效或已过期。")
            connection.execute("UPDATE users SET password_hash = ? WHERE id = ?", (password_hash, row["id"]))
            connection.execute(
                "UPDATE auth_sessions SET revoked_at = ? WHERE user_id = ? AND revoked_at IS NULL",
                (time.time(), row["id"]),
            )
            connection.execute(
                "UPDATE device_grants SET status = 'denied' "
                "WHERE user_id = ? AND status = 'approved' AND consumed_at IS NULL",
                (row["id"],),
            )
        return self._identity(row)

    def create_session(self, user_id: str, kind: str, *, ttl_seconds: int = 2_592_000) -> str:
        token = self.new_secret()
        now = time.time()
        with self._connection(immediate=True) as connection:
            connection.execute(
                """INSERT INTO auth_sessions
                (id, user_id, kind, token_hash, created_at, expires_at, last_seen_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (uuid4().hex, user_id, kind, self._token_hash(token), now, now + ttl_seconds, now),
            )
        return token

    def resolve_token(self, token: str) -> tuple[UserIdentity, str] | None:
        token_hash = self._token_hash(token)
        now = time.time()
        with self._connection(immediate=True) as connection:
            row = connection.execute(
                """SELECT s.kind, u.id, u.email, u.legacy_owner
                FROM auth_sessions AS s JOIN users AS u ON u.id = s.user_id
                WHERE s.token_hash = ? AND s.revoked_at IS NULL AND s.expires_at > ?""",
                (token_hash, now),
            ).fetchone()
            if row is None:
                return None
            connection.execute("UPDATE auth_sessions SET last_seen_at = ? WHERE token_hash = ?", (now, token_hash))
        return self._identity(row), str(row["kind"])

    def revoke_token(self, token: str) -> None:
        with self._connection(immediate=True) as connection:
            connection.execute(
                "UPDATE auth_sessions SET revoked_at = ? WHERE token_hash = ? AND revoked_at IS NULL",
                (time.time(), self._token_hash(token)),
            )

    def revoke_user_sessions(self, user_id: str) -> None:
        with self._connection(immediate=True) as connection:
            connection.execute(
                "UPDATE auth_sessions SET revoked_at = ? WHERE user_id = ? AND revoked_at IS NULL",
                (time.time(), user_id),
            )

    def start_device(self, server_url: str, *, ttl_seconds: int = 600) -> tuple[str, str, int]:
        poll = self.new_secret()
        browser = self.new_secret()
        now = time.time()
        with self._connection(immediate=True) as connection:
            connection.execute(
                """INSERT INTO device_grants
                (id, poll_hash, browser_hash, server_url, created_at, expires_at)
                VALUES (?, ?, ?, ?, ?, ?)""",
                (uuid4().hex, self._token_hash(poll), self._token_hash(browser), server_url, now, now + ttl_seconds),
            )
        return poll, browser, ttl_seconds

    def device_info(self, browser_secret: str) -> dict | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT server_url, created_at, expires_at, status FROM device_grants WHERE browser_hash = ?",
                (self._token_hash(browser_secret),),
            ).fetchone()
        if row is None or float(row["expires_at"]) <= time.time():
            return None
        return {
            "server_url": str(row["server_url"]),
            "created_at": float(row["created_at"]),
            "status": str(row["status"]),
        }

    def approve_device(self, browser_secret: str, user_id: str, approved: bool) -> bool:
        now = time.time()
        with self._connection(immediate=True) as connection:
            row = connection.execute(
                "SELECT id, expires_at, status FROM device_grants WHERE browser_hash = ?",
                (self._token_hash(browser_secret),),
            ).fetchone()
            if row is None or float(row["expires_at"]) <= now or str(row["status"]) != "pending":
                return False
            connection.execute(
                "UPDATE device_grants SET status = ?, user_id = ?, approved_at = ? WHERE id = ?",
                ("approved" if approved else "denied", user_id, now, row["id"]),
            )
        return True

    def poll_device(self, poll_secret: str) -> tuple[str, str | None]:
        now = time.time()
        with self._connection(immediate=True) as connection:
            row = connection.execute(
                "SELECT id, user_id, expires_at, status, consumed_at FROM device_grants WHERE poll_hash = ?",
                (self._token_hash(poll_secret),),
            ).fetchone()
            if row is None:
                return "invalid", None
            if float(row["expires_at"]) <= now:
                connection.execute("UPDATE device_grants SET status = 'expired' WHERE id = ?", (row["id"],))
                return "expired", None
            status = str(row["status"])
            if status == "pending":
                return "pending", None
            if status != "approved" or row["user_id"] is None or row["consumed_at"] is not None:
                return status, None
            token = self.new_secret()
            connection.execute(
                """INSERT INTO auth_sessions
                (id, user_id, kind, token_hash, created_at, expires_at, last_seen_at)
                VALUES (?, ?, 'device', ?, ?, ?, ?)""",
                (uuid4().hex, row["user_id"], self._token_hash(token), now, now + 2_592_000, now),
            )
            connection.execute("UPDATE device_grants SET consumed_at = ? WHERE id = ?", (now, row["id"]))
        return "approved", token

    def metadata(self, key: str) -> str | None:
        with self._connection() as connection:
            row = connection.execute("SELECT value FROM app_metadata WHERE key = ?", (key,)).fetchone()
        return str(row[0]) if row else None

    def set_metadata(self, key: str, value: str) -> None:
        with self._connection(immediate=True) as connection:
            connection.execute(
                "INSERT INTO app_metadata(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

def _key_material() -> bytes:
    configured = os.environ.get("MINI_AGENT_SECRET_KEY", "")
    if configured:
        return hashlib.sha256(configured.encode()).digest()
    try:
        login = os.getlogin()
    except OSError:
        login = ""
    return hashlib.sha256(f"{Path.home()}:{login}".encode()).digest()


def _encrypt_secret(value: str) -> str:
    if not value:
        return ""
    nonce = secrets.token_bytes(16)
    key = _key_material()
    raw = value.encode("utf-8")
    stream = bytes(byte ^ key[index % len(key)] for index, byte in enumerate(raw))
    return base64.urlsafe_b64encode(nonce + stream).decode("ascii")


def _decrypt_secret(value: str) -> str:
    if not value:
        return ""
    try:
        raw = base64.urlsafe_b64decode(value.encode("ascii"))
    except (ValueError, UnicodeError):
        return ""
    key = _key_material()
    payload = raw[16:]
    decoded = bytes(byte ^ key[index % len(key)] for index, byte in enumerate(payload))
    return decoded.decode("utf-8", errors="replace")