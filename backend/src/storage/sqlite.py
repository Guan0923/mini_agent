"""Public SQLite session-store facade.

The facade preserves the historical ``SQLiteSessionStore`` import while the
connection, session, and runtime responsibilities live in focused mixins.
"""

from __future__ import annotations

from .sqlite_approvals import SQLiteApprovalMixin
from .sqlite_base import SQLiteBaseMixin
from .sqlite_runtime import SQLiteRuntimeMixin
from .sqlite_schema import SQLiteSchemaMixin
from .sqlite_sessions import SQLiteSessionMixin
from .sqlite_sidebar_threads import SQLiteSidebarThreadMixin
from .sqlite_sync import SQLiteSyncMixin


class SQLiteSessionStore(
    SQLiteBaseMixin,
    SQLiteApprovalMixin,
    SQLiteSessionMixin,
    SQLiteSidebarThreadMixin,
    SQLiteRuntimeMixin,
    SQLiteSyncMixin,
    SQLiteSchemaMixin,
):
    """Local durable store; each session owns one self-contained state.db."""


__all__ = ["SQLiteSessionStore"]
