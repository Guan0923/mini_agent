"""Ports required by durable conversation orchestration."""

from __future__ import annotations

from typing import Protocol

from backend.domain import RunProvenance, RunStatus, RuntimeMessage, Session, SessionSummary

from ..core.context import RuntimeState
from ..core.ports import RuntimeStore


class TaskPreprocessor(Protocol):
    """Prepare one user task before it enters the runtime."""

    def expand(self, task: str) -> str: ...


class SessionStore(RuntimeStore, Protocol):
    """Persist session metadata, messages, runs, and resumable state."""

    def create_session(self, title: str | None = None) -> Session: ...

    def get_session(self, session_id: str) -> Session | None: ...

    def get_session_summary(self, session_id: str) -> SessionSummary | None: ...

    def list_sessions(self) -> list[SessionSummary]: ...

    def latest_session(self) -> Session | None: ...

    def load_conversation(self, session_id: str) -> list[dict[str, str]]: ...

    def load_conversation_page(
        self, session_id: str, *, before_id: int | None = None, limit: int = 100
    ) -> tuple[list[dict[str, str]], int | None]: ...

    def load_runtime(self, session_id: str) -> RuntimeState | None: ...

    def resume_runtime(self, source: RuntimeState, resumed: RuntimeState) -> None: ...

    def load_runtime_messages(self, session_id: str, run_id: str | None = None) -> list[RuntimeMessage]: ...

    def start_turn(
        self,
        session_id: str,
        run_id: str,
        task: str,
        provenance: RunProvenance | None = None,
        *,
        append_user_message: bool = True,
    ) -> None: ...

    def append_turn_input(self, session_id: str, run_id: str, content: str) -> None: ...

    def finish_turn(self, session_id: str, run_id: str, status: RunStatus, answer: str | None) -> None: ...
