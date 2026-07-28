"""Application-level dependency bundle exposed to interface adapters."""

from __future__ import annotations

from dataclasses import dataclass

from ..conversation.ports import SessionStore, TaskPreprocessor
from ..conversation.service import ConversationService
from ..execution import RuntimeRunner


@dataclass(frozen=True)
class AgentApplication:
    runner: RuntimeRunner
    session_store: SessionStore
    task_preprocessor: TaskPreprocessor
    sync_coordinator: object | None = None

    def close(self) -> None:
        from backend.mcp.client import close_external_tools

        close = getattr(self.sync_coordinator, "close", None)
        if callable(close):
            close(timeout=5.0)
        close_external_tools()

    def open_conversation(self, session_id: str | None = None) -> ConversationService:
        return ConversationService(self.runner, self.session_store, self.task_preprocessor, session_id)
