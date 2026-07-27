"""Session selection, persistence, and history projection for conversations."""

from __future__ import annotations

from typing import TYPE_CHECKING

from backend.domain import (
    DEFAULT_SESSION_TITLE,
    DEFAULT_TIME_ZONE,
    Session,
    SessionSummary,
    message_from_dict,
    validate_time_zone,
)

if TYPE_CHECKING:
    from backend.planning.context_management import ContextCompactionResult

from ..core.context import AgentRuntime, text_messages
from ..execution import RuntimeRunner
from .store import SessionStore


class ConversationSessionController:
    """Own the active durable session independently of turn execution."""

    def __init__(
        self,
        runner: RuntimeRunner,
        session_store: SessionStore | None = None,
        session_id: str | None = None,
    ) -> None:
        self.runner = runner
        self.session_store = session_store
        self._pending_session = False
        self._pending_title: str | None = None
        self.active_session: Session | None = None
        self.runtime: AgentRuntime | None = None
        self.conversation: list[dict[str, str]] = []
        if session_id is not None:
            self.use_session(session_id)

    def ensure_session(self, title: str | None = None) -> Session:
        if self.session_store is None:
            raise RuntimeError("Session storage is not configured.")
        if self.active_session is None:
            session_title = self._pending_title if self._pending_session and self._pending_title is not None else title
            return self._create_session(session_title)
        return self.active_session

    def new_session(self, title: str | None = None) -> Session:
        """Create and persist a session immediately for runtime-owned workflows."""

        return self._create_session(title)

    def prepare_new_session(self, title: str | None = None) -> None:
        """Detach the current context and defer persistence until the first task."""

        if self.session_store is None:
            raise RuntimeError("Session storage is not configured.")
        self._pending_session = True
        self._pending_title = title
        self.active_session = None
        self.runtime = None
        self.conversation = []

    @property
    def pending_session_title(self) -> str | None:
        if not self._pending_session:
            return None
        value = " ".join((self._pending_title or "").split())
        return value[:80] or DEFAULT_SESSION_TITLE

    @property
    def current_timezone(self) -> str:
        """Return the selected zone, including the default before a session exists."""

        if self.runtime is None:
            return DEFAULT_TIME_ZONE
        return self.runtime.state.timezone

    def set_timezone(self, timezone: str) -> str:
        """Persist a supported time zone for the active session."""

        selected = validate_time_zone(timezone)
        self.ensure_session()
        assert self.runtime is not None
        if self.runtime.state.status == "running":
            raise RuntimeError("Cannot change the time zone while a run is active.")
        self.runtime.state.timezone = selected
        self.runtime.save()
        self._reload_active_session()
        return selected

    def use_session(self, session_id: str) -> Session:
        if self.session_store is None:
            raise RuntimeError("Session storage is not configured.")
        session = self.session_store.get_session(session_id)
        if session is None:
            raise ValueError(f"Unknown session: {session_id}")
        self.active_session = session
        self._ensure_runtime(session_id)
        assert self.runtime is not None
        self.conversation = text_messages(self.runtime.state.messages)
        self._clear_pending_session()
        return session

    def list_sessions(self) -> list[SessionSummary]:
        if self.session_store is None:
            raise RuntimeError("Session storage is not configured.")
        return self.session_store.list_sessions()

    def current_summary(self) -> SessionSummary | None:
        if self.session_store is None or self.active_session is None:
            return None
        return self.session_store.get_session_summary(self.active_session.session_id)

    def compact_context(self) -> ContextCompactionResult:
        if self.runtime is None:
            from backend.planning.context_management import ContextCompactionResult

            return ContextCompactionResult(False, 0, 0)
        result = self.runner.compact_context(self.runtime)
        self.conversation = text_messages(self.runtime.state.messages)
        return result

    def history(self) -> list[dict[str, str]]:
        if self.runtime is None:
            return []
        return text_messages(self.runtime.state.messages)

    def history_page(
        self, *, before_id: int | None = None, limit: int = 100
    ) -> tuple[list[dict[str, str]], int | None]:
        if self.active_session is None or self.session_store is None:
            return (self.history()[-limit:], None)
        load_page = getattr(self.session_store, "load_conversation_page", None)
        if callable(load_page):
            return load_page(self.active_session.session_id, before_id=before_id, limit=limit)
        return (self.history()[-limit:], None)

    def _ensure_runtime(self, session_id: str) -> None:
        if self.runtime is not None and self.runtime.state.session_id == session_id:
            return
        assert self.session_store is not None
        state = self.session_store.load_runtime(session_id)
        if state is None:
            legacy = [message_from_dict(item) for item in self.session_store.load_conversation(session_id)]
            self.runtime = self.runner.empty_runtime(
                session_id=session_id,
                messages=legacy,
                runtime_store=self.session_store,
            )
            self.runtime.save()
            return
        runtime = self.runner.empty_runtime(session_id=session_id, runtime_store=self.session_store)
        runtime.state = state
        self.runtime = self.runner.bind(runtime)

    def _create_session(self, title: str | None) -> Session:
        if self.session_store is None:
            raise RuntimeError("Session storage is not configured.")
        session = self.session_store.create_session(title)
        runtime = self.runner.empty_runtime(session_id=session.session_id, runtime_store=self.session_store)
        runtime.save()
        self.active_session = session
        self.runtime = runtime
        self.conversation = []
        self._clear_pending_session()
        return session

    def _clear_pending_session(self) -> None:
        self._pending_session = False
        self._pending_title = None

    def _reload_active_session(self) -> None:
        assert self.session_store is not None and self.active_session is not None
        self.active_session = self.session_store.get_session(self.active_session.session_id)
