"""SQLite identity, password, and verification-challenge operations."""

from __future__ import annotations

import sqlite3
import time
from uuid import uuid4

from .types import UserIdentity


class AuthIdentityMixin:
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
