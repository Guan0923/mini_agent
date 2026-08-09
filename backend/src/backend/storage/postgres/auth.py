"""PostgreSQL-backed Web identity and authentication repository."""

from __future__ import annotations

import hashlib
import secrets
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row
from pwdlib import PasswordHash

from backend.storage.auth.types import AuthStorageUnavailable, UserIdentity

from .auth_schema import AUTH_SCHEMA_STATEMENTS, AUTH_SCHEMA_VERSION


class PostgresAuthRepository:
    """Authoritative Web authentication store; never falls back to local state."""

    def __init__(self, database_url: str, *, connect_timeout: int = 5) -> None:
        if not database_url.strip():
            raise ValueError("DATABASE_URL is required for Web authentication.")
        self.database_url = database_url
        self.connect_timeout = connect_timeout
        self.passwords = PasswordHash.recommended()
        self.initialize()

    @contextmanager
    def connection(self) -> Iterator[psycopg.Connection[dict[str, Any]]]:
        try:
            with psycopg.connect(
                self.database_url,
                connect_timeout=self.connect_timeout,
                row_factory=dict_row,
            ) as connection:
                yield connection
        except psycopg.IntegrityError:
            raise
        except psycopg.Error as exc:
            raise AuthStorageUnavailable("认证数据库暂不可用。") from exc

    def initialize(self) -> None:
        with self.connection() as connection:
            connection.execute(AUTH_SCHEMA_STATEMENTS[0])
            row = connection.execute("SELECT MAX(version) AS version FROM web_auth_schema_migrations").fetchone()
            applied_version = int(row["version"] or 0) if row else 0
            if applied_version > AUTH_SCHEMA_VERSION:
                raise RuntimeError(
                    f"PostgreSQL Web auth schema version {applied_version} is newer than supported "
                    f"version {AUTH_SCHEMA_VERSION}."
                )
            for statement in AUTH_SCHEMA_STATEMENTS[1:]:
                connection.execute(statement)
            connection.execute(
                "INSERT INTO web_auth_schema_migrations(version) VALUES (%s) ON CONFLICT(version) DO NOTHING",
                (AUTH_SCHEMA_VERSION,),
            )

    def ping(self) -> None:
        with self.connection() as connection:
            connection.execute("SELECT 1")

    def close(self) -> None:
        """Connections are scoped to individual transactions."""

    @staticmethod
    def _token_hash(value: str) -> str:
        return hashlib.sha256(value.encode()).hexdigest()

    @staticmethod
    def new_secret() -> str:
        return secrets.token_urlsafe(48)

    @staticmethod
    def _identity(row: dict[str, Any]) -> UserIdentity:
        return UserIdentity(str(row["id"]), str(row["email"]), bool(row["legacy_owner"]))

    @staticmethod
    def _lock(connection: psycopg.Connection, key: str) -> None:
        connection.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (key,))

    def password_hash(self, password: str) -> str:
        return self.passwords.hash(password)

    def verify_password(self, password: str, password_hash: str) -> bool:
        try:
            return self.passwords.verify(password, password_hash)
        except (ValueError, TypeError):
            return False

    def user_by_email(self, email: str) -> UserIdentity | None:
        with self.connection() as connection:
            row = connection.execute("SELECT id,email,legacy_owner FROM users WHERE email=%s", (email,)).fetchone()
        return self._identity(row) if row else None

    def user_by_id(self, user_id: str) -> UserIdentity | None:
        with self.connection() as connection:
            row = connection.execute("SELECT id,email,legacy_owner FROM users WHERE id=%s", (user_id,)).fetchone()
        return self._identity(row) if row else None

    def password_hash_for_user(self, email: str) -> str | None:
        with self.connection() as connection:
            row = connection.execute("SELECT password_hash FROM users WHERE email=%s", (email,)).fetchone()
        return str(row["password_hash"]) if row else None

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
        with self.connection() as connection:
            self._lock(connection, f"challenge:{email}:{purpose}")
            connection.execute(
                "UPDATE verification_challenges SET consumed_at=%s "
                "WHERE email=%s AND purpose=%s AND consumed_at IS NULL",
                (now, email, purpose),
            )
            connection.execute(
                """INSERT INTO verification_challenges
                (id,email,purpose,code_hash,ip_address,created_at,expires_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                (uuid4().hex, email, purpose, self.password_hash(code), ip_address, now, now + ttl_seconds),
            )

    def _limit_count(
        self,
        connection: psycopg.Connection,
        key: str,
        action: str,
        window_start: int,
    ) -> int:
        row = connection.execute(
            "SELECT count FROM rate_limits WHERE key=%s AND action=%s AND window_start=%s",
            (key, action, window_start),
        ).fetchone()
        return int(row["count"]) if row else 0

    @staticmethod
    def _increment_limit(
        connection: psycopg.Connection,
        key: str,
        action: str,
        window_start: int,
    ) -> None:
        connection.execute(
            """INSERT INTO rate_limits(key,action,window_start,count) VALUES (%s,%s,%s,1)
            ON CONFLICT(key,action,window_start) DO UPDATE SET count=rate_limits.count+1""",
            (key, action, window_start),
        )

    def can_send(self, email: str, purpose: str, ip_address: str | None) -> tuple[bool, int]:
        now = time.time()
        window_seconds = 3600
        window_start = int(now) - (int(now) % window_seconds)
        keys = [(f"email:{email}", 5)]
        if ip_address:
            keys.append((f"ip:{ip_address}", 20))
        action = f"code:{purpose}:hour"
        with self.connection() as connection:
            for key, _limit in sorted(keys):
                self._lock(connection, f"rate:{key}:{action}:{window_start}")
            self._lock(connection, f"challenge:{email}:{purpose}")
            latest = connection.execute(
                """SELECT created_at FROM verification_challenges
                WHERE email=%s AND purpose=%s ORDER BY created_at DESC LIMIT 1""",
                (email, purpose),
            ).fetchone()
            if latest:
                remaining = max(0, 60 - int(now - float(latest["created_at"])))
                if remaining:
                    return False, remaining
            for key, limit in keys:
                if self._limit_count(connection, key, action, window_start) >= limit:
                    return False, window_seconds
            for key, _limit in keys:
                self._increment_limit(connection, key, action, window_start)
        return True, 0

    def consume_limit(self, key: str, action: str, limit: int, window_seconds: int) -> bool:
        now = int(time.time())
        window_start = now - (now % window_seconds)
        with self.connection() as connection:
            self._lock(connection, f"rate:{key}:{action}:{window_start}")
            if self._limit_count(connection, key, action, window_start) >= limit:
                return False
            self._increment_limit(connection, key, action, window_start)
        return True

    def _consume_challenge(
        self,
        connection: psycopg.Connection,
        email: str,
        purpose: str,
        code: str,
    ) -> bool:
        row = connection.execute(
            """SELECT id,code_hash,expires_at,attempts FROM verification_challenges
            WHERE email=%s AND purpose=%s AND consumed_at IS NULL
            ORDER BY created_at DESC LIMIT 1 FOR UPDATE""",
            (email, purpose),
        ).fetchone()
        if row is None:
            return False
        now = time.time()
        if float(row["expires_at"]) <= now or int(row["attempts"]) >= 5:
            connection.execute("UPDATE verification_challenges SET consumed_at=%s WHERE id=%s", (now, row["id"]))
            return False
        if not self.verify_password(code, str(row["code_hash"])):
            attempts = int(row["attempts"]) + 1
            connection.execute(
                "UPDATE verification_challenges SET attempts=%s,consumed_at=%s WHERE id=%s",
                (attempts, now if attempts >= 5 else None, row["id"]),
            )
            return False
        connection.execute("UPDATE verification_challenges SET consumed_at=%s WHERE id=%s", (now, row["id"]))
        return True

    def register_user(self, email: str, code: str, password: str) -> UserIdentity:
        password_hash = self.password_hash(password)
        now = time.time()
        user_id = uuid4().hex
        try:
            with self.connection() as connection:
                self._lock(connection, f"challenge:{email}:register")
                if not self._consume_challenge(connection, email, "register", code):
                    raise ValueError("验证码无效或已过期。")
                if connection.execute("SELECT 1 FROM users WHERE email=%s", (email,)).fetchone():
                    raise ValueError("该邮箱已注册，请直接登录。")
                connection.execute(
                    "INSERT INTO users(id,email,password_hash,created_at,legacy_owner) VALUES (%s,%s,%s,%s,FALSE)",
                    (user_id, email, password_hash, now),
                )
        except psycopg.errors.UniqueViolation as exc:
            raise ValueError("该邮箱已注册，请直接登录。") from exc
        return UserIdentity(user_id, email, False)

    def authenticate(self, email: str, password: str) -> UserIdentity | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT id,email,password_hash,legacy_owner FROM users WHERE email=%s", (email,)
            ).fetchone()
        if row is None or not self.verify_password(password, str(row["password_hash"])):
            return None
        return self._identity(row)

    def reset_password(self, email: str, code: str, password: str) -> UserIdentity:
        password_hash = self.password_hash(password)
        now = time.time()
        with self.connection() as connection:
            self._lock(connection, f"challenge:{email}:reset")
            if not self._consume_challenge(connection, email, "reset", code):
                raise ValueError("验证码无效或已过期。")
            row = connection.execute(
                "SELECT id,email,legacy_owner FROM users WHERE email=%s FOR UPDATE", (email,)
            ).fetchone()
            if row is None:
                raise ValueError("验证码无效或已过期。")
            connection.execute("UPDATE users SET password_hash=%s WHERE id=%s", (password_hash, row["id"]))
            connection.execute(
                "UPDATE auth_sessions SET revoked_at=%s WHERE user_id=%s AND revoked_at IS NULL",
                (now, row["id"]),
            )
            connection.execute(
                "UPDATE device_grants SET status='denied' "
                "WHERE user_id=%s AND status='approved' AND consumed_at IS NULL",
                (row["id"],),
            )
        return self._identity(row)

    def create_session(self, user_id: str, kind: str, *, ttl_seconds: int = 2_592_000) -> str:
        token = self.new_secret()
        now = time.time()
        with self.connection() as connection:
            connection.execute(
                """INSERT INTO auth_sessions
                (id,user_id,kind,token_hash,created_at,expires_at,last_seen_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                (uuid4().hex, user_id, kind, self._token_hash(token), now, now + ttl_seconds, now),
            )
        return token

    def resolve_token(self, token: str) -> tuple[UserIdentity, str] | None:
        token_hash = self._token_hash(token)
        now = time.time()
        with self.connection() as connection:
            row = connection.execute(
                """SELECT s.kind,u.id,u.email,u.legacy_owner
                FROM auth_sessions AS s JOIN users AS u ON u.id=s.user_id
                WHERE s.token_hash=%s AND s.revoked_at IS NULL AND s.expires_at>%s""",
                (token_hash, now),
            ).fetchone()
            if row is None:
                return None
            connection.execute("UPDATE auth_sessions SET last_seen_at=%s WHERE token_hash=%s", (now, token_hash))
        return self._identity(row), str(row["kind"])

    def revoke_token(self, token: str) -> None:
        with self.connection() as connection:
            connection.execute(
                "UPDATE auth_sessions SET revoked_at=%s WHERE token_hash=%s AND revoked_at IS NULL",
                (time.time(), self._token_hash(token)),
            )

    def revoke_user_sessions(self, user_id: str) -> None:
        with self.connection() as connection:
            connection.execute(
                "UPDATE auth_sessions SET revoked_at=%s WHERE user_id=%s AND revoked_at IS NULL",
                (time.time(), user_id),
            )

    def start_device(self, server_url: str, *, ttl_seconds: int = 600) -> tuple[str, str, int]:
        poll = self.new_secret()
        browser = self.new_secret()
        now = time.time()
        with self.connection() as connection:
            connection.execute(
                """INSERT INTO device_grants
                (id,poll_hash,browser_hash,server_url,created_at,expires_at)
                VALUES (%s,%s,%s,%s,%s,%s)""",
                (
                    uuid4().hex,
                    self._token_hash(poll),
                    self._token_hash(browser),
                    server_url,
                    now,
                    now + ttl_seconds,
                ),
            )
        return poll, browser, ttl_seconds

    def device_info(self, browser_secret: str) -> dict[str, object] | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT server_url,created_at,expires_at,status FROM device_grants WHERE browser_hash=%s",
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
        with self.connection() as connection:
            row = connection.execute(
                "SELECT id,expires_at,status FROM device_grants WHERE browser_hash=%s FOR UPDATE",
                (self._token_hash(browser_secret),),
            ).fetchone()
            if row is None or float(row["expires_at"]) <= now or str(row["status"]) != "pending":
                return False
            connection.execute(
                "UPDATE device_grants SET status=%s,user_id=%s,approved_at=%s WHERE id=%s",
                ("approved" if approved else "denied", user_id, now, row["id"]),
            )
        return True

    def poll_device(self, poll_secret: str) -> tuple[str, str | None]:
        now = time.time()
        with self.connection() as connection:
            row = connection.execute(
                """SELECT id,user_id,expires_at,status,consumed_at FROM device_grants
                WHERE poll_hash=%s FOR UPDATE""",
                (self._token_hash(poll_secret),),
            ).fetchone()
            if row is None:
                return "invalid", None
            if float(row["expires_at"]) <= now:
                connection.execute("UPDATE device_grants SET status='expired' WHERE id=%s", (row["id"],))
                return "expired", None
            status = str(row["status"])
            if status == "pending":
                return "pending", None
            if status != "approved" or row["user_id"] is None or row["consumed_at"] is not None:
                return status, None
            token = self.new_secret()
            connection.execute(
                """INSERT INTO auth_sessions
                (id,user_id,kind,token_hash,created_at,expires_at,last_seen_at)
                VALUES (%s,%s,'device',%s,%s,%s,%s)""",
                (uuid4().hex, row["user_id"], self._token_hash(token), now, now + 2_592_000, now),
            )
            connection.execute("UPDATE device_grants SET consumed_at=%s WHERE id=%s", (now, row["id"]))
        return "approved", token

    def metadata(self, key: str) -> str | None:
        with self.connection() as connection:
            row = connection.execute("SELECT value FROM app_metadata WHERE key=%s", (key,)).fetchone()
        return str(row["value"]) if row else None

    def set_metadata(self, key: str, value: str) -> None:
        with self.connection() as connection:
            connection.execute(
                """INSERT INTO app_metadata(key,value) VALUES (%s,%s)
                ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value""",
                (key, value),
            )


__all__ = ["PostgresAuthRepository"]
