"""Local browser/session identity store for the client-side backend.

Passwords, verification codes, and cloud account authority do not live here.
Only short-lived local session hashes and cached identity metadata are stored.
"""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID, uuid4

from .types import UserIdentity

LOCAL_AUTH_SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS local_identities (
    id TEXT PRIMARY KEY,
    email TEXT,
    kind TEXT NOT NULL CHECK(kind IN ('account', 'guest')),
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS local_device_state (
    id INTEGER PRIMARY KEY CHECK(id = 1),
    guest_id TEXT REFERENCES local_identities(id) ON DELETE SET NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS local_sessions (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES local_identities(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    last_seen_at REAL NOT NULL,
    revoked_at REAL
);

CREATE INDEX IF NOT EXISTS local_sessions_lookup_idx
    ON local_sessions(token_hash, expires_at, revoked_at);

CREATE TABLE IF NOT EXISTS guest_imports (
    target_user_id TEXT PRIMARY KEY REFERENCES local_identities(id) ON DELETE CASCADE,
    guest_user_id TEXT NOT NULL REFERENCES local_identities(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS local_rate_limits (
    key TEXT NOT NULL,
    action TEXT NOT NULL,
    window_start INTEGER NOT NULL,
    count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(key, action, window_start)
);
"""


class LocalAuthStore:
    """Own local sessions without becoming an account database."""

    def __init__(self, path: Path) -> None:
        path = Path(path)
        if path.name != "client.db":
            raise ValueError("Local auth database must be named client.db.")
        if path.is_symlink():
            raise ValueError("Local auth database cannot be a symbolic link.")
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self._connection() as connection:
            connection.executescript(LOCAL_AUTH_SCHEMA)

    @contextmanager
    def _connection(self, *, immediate: bool = False):
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

    @staticmethod
    def _identity(row: sqlite3.Row) -> UserIdentity:
        return UserIdentity(str(row["id"]), str(row["email"]) if row["email"] else None, str(row["kind"]))

    @staticmethod
    def _validate_identity(identity: UserIdentity) -> None:
        if identity.kind not in {"account", "guest"}:
            raise ValueError("Local identity kind must be account or guest.")
        try:
            parsed = UUID(identity.id)
        except (AttributeError, ValueError) as exc:
            raise ValueError("Local identity id must be a canonical UUID.") from exc
        if str(parsed) != identity.id:
            raise ValueError("Local identity id must be a canonical UUID.")

    def upsert_identity(self, identity: UserIdentity) -> UserIdentity:
        self._validate_identity(identity)
        now = time.time()
        with self._connection(immediate=True) as connection:
            connection.execute(
                """INSERT INTO local_identities(id,email,kind,created_at,updated_at)
                VALUES (?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET email=excluded.email,
                kind=excluded.kind,updated_at=excluded.updated_at""",
                (identity.id, identity.email, identity.kind, now, now),
            )
        return identity

    def user_by_id(self, user_id: str) -> UserIdentity | None:
        with self._connection() as connection:
            row = connection.execute("SELECT id,email,kind FROM local_identities WHERE id=?", (user_id,)).fetchone()
        return self._identity(row) if row is not None else None

    def user_by_email(self, email: str) -> UserIdentity | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT id,email,kind FROM local_identities WHERE email=? AND kind='account'", (email,)
            ).fetchone()
        return self._identity(row) if row is not None else None

    def get_or_create_guest(self) -> tuple[UserIdentity, bool]:
        """Return the one device-scoped guest, creating it atomically.

        Older client databases may contain more than one guest because the
        browser session used to be the only identity anchor.  Keep those
        records available for explicit cleanup/recovery, but deterministically
        select the oldest one as the canonical device guest on first access.
        """

        now = time.time()
        with self._connection(immediate=True) as connection:
            row = connection.execute(
                """SELECT u.id,u.email,u.kind FROM local_device_state d
                JOIN local_identities u ON u.id=d.guest_id
                WHERE d.id=1 AND u.kind='guest'"""
            ).fetchone()
            if row is not None:
                return self._identity(row), False

            legacy = connection.execute(
                """SELECT id,email,kind FROM local_identities
                WHERE kind='guest' ORDER BY created_at ASC, id ASC LIMIT 1"""
            ).fetchone()
            created = legacy is None
            if legacy is None:
                identity = UserIdentity(str(uuid4()), None, "guest")
                connection.execute(
                    """INSERT INTO local_identities(id,email,kind,created_at,updated_at)
                    VALUES (?,?,?,?,?)""",
                    (identity.id, None, "guest", now, now),
                )
            else:
                identity = self._identity(legacy)
            connection.execute(
                """INSERT INTO local_device_state(id,guest_id,created_at,updated_at)
                VALUES (1,?,?,?)
                ON CONFLICT(id) DO UPDATE SET guest_id=excluded.guest_id,updated_at=excluded.updated_at""",
                (identity.id, now, now),
            )
            return identity, created

    def create_guest(self) -> UserIdentity:
        """Compatibility wrapper returning the canonical device guest."""

        return self.get_or_create_guest()[0]

    def delete_guest(self, user_id: str) -> None:
        with self._connection(immediate=True) as connection:
            connection.execute("DELETE FROM local_identities WHERE id=? AND kind='guest'", (user_id,))

    def create_session(self, user_id: str, kind: str = "browser", *, ttl_seconds: int = 2_592_000) -> str:
        if kind not in {"browser", "device"}:
            raise ValueError("Local session kind must be browser or device.")
        if ttl_seconds <= 0:
            raise ValueError("Local session TTL must be greater than zero.")
        token = self.new_secret()
        now = time.time()
        with self._connection(immediate=True) as connection:
            exists = connection.execute("SELECT 1 FROM local_identities WHERE id=?", (user_id,)).fetchone()
            if exists is None:
                raise ValueError("Unknown local identity.")
            connection.execute(
                """INSERT INTO local_sessions
                (id,user_id,kind,token_hash,created_at,expires_at,last_seen_at)
                VALUES (?,?,?,?,?,?,?)""",
                (uuid4().hex, user_id, kind, self._token_hash(token), now, now + ttl_seconds, now),
            )
        return token

    def create_guest_session(self, user_id: str, *, ttl_seconds: int = 2_592_000) -> str:
        self.upsert_identity(UserIdentity(user_id, None, "guest"))
        return self.create_session(user_id, "browser", ttl_seconds=ttl_seconds)

    def resolve_token(self, token: str) -> tuple[UserIdentity, str] | None:
        now = time.time()
        with self._connection(immediate=True) as connection:
            row = connection.execute(
                """SELECT s.kind,u.id,u.email,u.kind AS identity_kind FROM local_sessions s
                JOIN local_identities u ON u.id=s.user_id
                WHERE s.token_hash=? AND s.revoked_at IS NULL AND s.expires_at>?""",
                (self._token_hash(token), now),
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                "UPDATE local_sessions SET last_seen_at=? WHERE token_hash=?",
                (now, self._token_hash(token)),
            )
        identity = UserIdentity(str(row["id"]), str(row["email"]) if row["email"] else None, str(row["identity_kind"]))
        return identity, str(row["kind"])

    def revoke_token(self, token: str) -> None:
        with self._connection(immediate=True) as connection:
            connection.execute(
                "UPDATE local_sessions SET revoked_at=? WHERE token_hash=? AND revoked_at IS NULL",
                (time.time(), self._token_hash(token)),
            )

    def revoke_user_sessions(self, user_id: str) -> None:
        with self._connection(immediate=True) as connection:
            connection.execute(
                "UPDATE local_sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL",
                (time.time(), user_id),
            )

    def set_pending_guest_import(self, user_id: str, guest_id: str) -> None:
        now = time.time()
        with self._connection(immediate=True) as connection:
            connection.execute(
                """INSERT INTO guest_imports(target_user_id,guest_user_id,status,created_at,updated_at)
                VALUES (?,?, 'pending', ?, ?) ON CONFLICT(target_user_id) DO UPDATE SET
                guest_user_id=CASE WHEN guest_imports.status='pending' THEN excluded.guest_user_id ELSE guest_imports.guest_user_id END,
                updated_at=excluded.updated_at""",
                (user_id, guest_id, now, now),
            )

    def pending_guest_import(self, user_id: str) -> dict[str, object] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT guest_user_id,status,created_at,updated_at FROM guest_imports WHERE target_user_id=?",
                (user_id,),
            ).fetchone()
        if row is None or str(row["status"]) != "pending":
            return None
        return {
            "guest_id": str(row["guest_user_id"]),
            "status": "pending",
            "created_at": float(row["created_at"]),
            "updated_at": float(row["updated_at"]),
        }

    def finish_guest_import(self, user_id: str, decision: str) -> None:
        if decision not in {"import", "dismiss"}:
            raise ValueError("decision must be import or dismiss")
        with self._connection(immediate=True) as connection:
            connection.execute(
                "UPDATE guest_imports SET status=?,updated_at=? WHERE target_user_id=? AND status='pending'",
                ("imported" if decision == "import" else "dismissed", time.time(), user_id),
            )

    def consume_limit(self, key: str, action: str, limit: int, window_seconds: int) -> bool:
        now = int(time.time())
        window_start = now - now % window_seconds
        with self._connection(immediate=True) as connection:
            row = connection.execute(
                "SELECT count FROM local_rate_limits WHERE key=? AND action=? AND window_start=?",
                (key, action, window_start),
            ).fetchone()
            count = int(row[0]) if row else 0
            if count >= limit:
                return False
            if row:
                connection.execute(
                    "UPDATE local_rate_limits SET count=? WHERE key=? AND action=? AND window_start=?",
                    (count + 1, key, action, window_start),
                )
            else:
                connection.execute(
                    "INSERT INTO local_rate_limits(key,action,window_start,count) VALUES (?,?,?,1)",
                    (key, action, window_start),
                )
        return True

    def ping(self) -> None:
        with self._connection() as connection:
            connection.execute("SELECT 1")

    def close(self) -> None:
        return None


__all__ = ["LocalAuthStore", "LOCAL_AUTH_SCHEMA"]
