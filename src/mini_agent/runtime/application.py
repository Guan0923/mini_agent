"""Application-level dependency bundle exposed to interface adapters."""

from __future__ import annotations

from dataclasses import dataclass

from .conversations import ConversationService, TaskPreprocessor
from .execution import RuntimeRunner
from .session_store import SessionStore


@dataclass(frozen=True)
class AgentApplication:
    """Open interface-neutral conversation services from one composed runtime."""

    runner: RuntimeRunner
    session_store: SessionStore
    task_preprocessor: TaskPreprocessor

    def open_conversation(self, session_id: str | None = None) -> ConversationService:
        return ConversationService(self.runner, self.session_store, self.task_preprocessor, session_id)
