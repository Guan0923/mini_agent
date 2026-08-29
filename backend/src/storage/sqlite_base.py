"""SQLite connection and common persistence lifecycle helpers."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager

from backend.configuration import ClientPaths

from .sqlite_schema import SCHEMA


class SQLiteBaseMixin:
    def __init__(self, paths: ClientPaths, device_id: str) -> None:
        self.paths = paths
        self.device_id = device_id
        self.paths.ensure()
        self._sync_listener = None

    @contextmanager
    def _connection(self, session_id: str) -> Iterator[sqlite3.Connection]:
        path = self.paths.session_db(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(path)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            # Inspect before DDL: unsupported databases remain untouched.
            self._assert_supported_schema(connection)
            connection.executescript(SCHEMA)
            self._migrate_schema(connection)
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def set_sync_listener(self, listener) -> None:
        self._sync_listener = listener

    def _is_local_only(self, session_id: str) -> bool:
        session = self.get_session(session_id)
        return bool(session and session.local_only)
