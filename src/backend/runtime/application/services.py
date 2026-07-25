"""Application-level dependency bundle exposed to interface adapters."""

from __future__ import annotations

from dataclasses import dataclass

from backend.storage import PostgresDatabase

from ..conversation.service import ConversationService, TaskPreprocessor
from ..conversation.store import SessionStore
from ..execution import RuntimeRunner


@dataclass(frozen=True)
class AgentApplication:
    """Open interface-neutral conversation services from one composed runtime."""

    runner: RuntimeRunner
    session_store: SessionStore
    task_preprocessor: TaskPreprocessor
    database: PostgresDatabase

    def close(self) -> None:
        """Release the application-owned PostgreSQL connection pool."""

        self.database.close()

    def open_conversation(self, session_id: str | None = None) -> ConversationService:
        return ConversationService(self.runner, self.session_store, self.task_preprocessor, session_id)
