"""Browser sessions and device-authorization token operations."""

from __future__ import annotations

import time
from uuid import uuid4

from .types import UserIdentity


class AuthTokenMixin:
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
