"""Public SQLite session-store facade.

The facade preserves the historical ``SQLiteSessionStore`` import while the
connection, session, and runtime responsibilities live in focused mixins.
"""

from __future__ import annotations

from .sqlite_base import SQLiteBaseMixin
from .sqlite_fork import SQLiteForkMixin
from .sqlite_runtime import SQLiteRuntimeMixin
from .sqlite_schema import SQLiteSchemaMixin
from .sqlite_sessions import SQLiteSessionMixin
from .sqlite_sync import SQLiteSyncMixin


class SQLiteSessionStore(
    SQLiteBaseMixin,
    SQLiteSessionMixin,
    SQLiteRuntimeMixin,
    SQLiteForkMixin,
    SQLiteSyncMixin,
    SQLiteSchemaMixin,
):
    """Local durable store; each session owns one self-contained state.db."""


__all__ = ["SQLiteSessionStore"]
