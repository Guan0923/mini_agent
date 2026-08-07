"""SQLite connection and schema ownership for authentication."""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from pwdlib import PasswordHash

from .schema import SCHEMA


class AuthDatabaseMixin:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.passwords = PasswordHash.recommended()
        with self._connection() as connection:
            connection.executescript(SCHEMA)
            self._migrate_schema(connection)

    @staticmethod
    def _migrate_schema(connection: sqlite3.Connection) -> None:
        columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(user_agent_settings)")}
        migrations = (
            ("display_mode", "ALTER TABLE user_agent_settings ADD COLUMN display_mode TEXT NOT NULL DEFAULT 'medium'"),
            ("timezone", "ALTER TABLE user_agent_settings ADD COLUMN timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai'"),
            (
                "location_enabled",
                "ALTER TABLE user_agent_settings ADD COLUMN location_enabled INTEGER NOT NULL DEFAULT 0",
            ),
        )
        for name, statement in migrations:
            if name not in columns:
                connection.execute(statement)

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
