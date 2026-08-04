"""Durable, secret-safe authentication repository backed by SQLite."""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from pwdlib import PasswordHash

from .auth_schema import SCHEMA
from .auth_types import UserIdentity


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
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

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
