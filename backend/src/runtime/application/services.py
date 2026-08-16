"""Application-level dependency bundle exposed to interface adapters."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from backend.domain import DEFAULT_TIME_ZONE

from ..conversation.ports import SessionStore, TaskPreprocessor
from ..conversation.service import ConversationService
from ..execution import RuntimeRunner


@dataclass(frozen=True)
class AgentApplication:
    runner: RuntimeRunner
    session_store: SessionStore
    task_preprocessor: TaskPreprocessor
    sync_coordinator: object | None = None
    default_timezone: str = DEFAULT_TIME_ZONE
    session_provisioner: Callable[..., object] | None = None
    session_provisioner_cleanup: Callable[[str], None] | None = None
    project_id: str | None = None

    def close(self) -> None:
        try:
            close = getattr(self.sync_coordinator, "close", None)
            if callable(close):
                close(timeout=5.0)
        finally:
            runner_close = getattr(self.runner, "close", None)
            if callable(runner_close):
                runner_close()

    def open_conversation(self, session_id: str | None = None) -> ConversationService:
        return ConversationService(
            self.runner,
            self.session_store,
            self.task_preprocessor,
            session_id,
            self.default_timezone,
            session_provisioner=self.session_provisioner,
            session_provisioner_cleanup=self.session_provisioner_cleanup,
        )
