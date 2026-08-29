"""SQLite connection and common persistence lifecycle helpers."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager

from backend.configuration import ClientPaths

from .sqlite_schema import SCHEMA


class SQLiteBaseMixin:
    def __init__(self, paths: ClientPaths, agent_thread_index: object | None = None) -> None:
        self.paths = paths
        self.agent_thread_index = agent_thread_index
        self.paths.ensure()

    @contextmanager
    def _connection(self, session_id: str) -> Iterator[sqlite3.Connection]:
        path = self.paths.session_db(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(path)
        committed = False
        changed = False
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            # Inspect before DDL: unsupported databases remain untouched.
            self._assert_supported_schema(connection)
            self._prepare_schema(connection)
            connection.executescript(SCHEMA)
            self._validate_schema(connection)
            baseline_changes = connection.total_changes
            yield connection
            changed = connection.total_changes > baseline_changes
            connection.commit()
            committed = True
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        if committed and changed and self.agent_thread_index is not None:
            refresh = getattr(self.agent_thread_index, "refresh_session", None)
            if callable(refresh):
                refresh(self, session_id)
