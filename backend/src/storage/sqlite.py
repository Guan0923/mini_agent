"""Public SQLite session-store facade.

The facade preserves the historical ``SQLiteSessionStore`` import while the
connection, session, and runtime responsibilities live in focused mixins.
"""

from __future__ import annotations

from .sqlite_agent_threads import SQLiteAgentThreadMixin
from .sqlite_approvals import SQLiteApprovalMixin
from .sqlite_base import SQLiteBaseMixin
from .sqlite_right_panel import SQLiteRightPanelMixin
from .sqlite_runtime import SQLiteRuntimeMixin
from .sqlite_schema import SQLiteSchemaMixin
from .sqlite_sessions import SQLiteSessionMixin
from .sqlite_sidebar_threads import SQLiteSidebarThreadMixin


class SQLiteSessionStore(
    SQLiteBaseMixin,
    SQLiteApprovalMixin,
    SQLiteAgentThreadMixin,
    SQLiteSessionMixin,
    SQLiteSidebarThreadMixin,
    SQLiteRightPanelMixin,
    SQLiteRuntimeMixin,
    SQLiteSchemaMixin,
):
    """Local durable store; each session owns one self-contained state.db."""


__all__ = ["SQLiteSessionStore"]
