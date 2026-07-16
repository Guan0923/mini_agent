"""Application service for one persistent AgentRuntime conversation."""

from __future__ import annotations

from typing import Protocol

from mini_agent.domain import (
    DEFAULT_SESSION_TITLE,
    AssistantMessage,
    RunHandoff,
    RunMode,
    RunState,
    Session,
    SessionSummary,
    UserMessage,
    message_from_dict,
    new_run_id,
    new_session_id,
)
from mini_agent.tools import ToolError

from .context import AgentRuntime, text_messages
from .contracts import CancellationHandler, EventHandler, InterruptHandler, SteeringHandler
from .events import RuntimeEvent
from .execution import RuntimeRunner
from .session_store import SessionStore


class TaskPreprocessor(Protocol):
    def expand(self, task: str) -> str: ...


class TaskPreparationError(ValueError):
    pass


class ConversationService:
    def __init__(
        self,
        runner: RuntimeRunner,
        session_store: SessionStore | None = None,
        task_preprocessor: TaskPreprocessor | None = None,
        session_id: str | None = None,
    ) -> None:
        self.runner = runner
        self.session_store = session_store
        self._task_preprocessor = task_preprocessor
        self._pending_session = False
        self._pending_title: str | None = None
        self.active_session: Session | None = None
        self.runtime: AgentRuntime | None = None
        self.conversation: list[dict[str, str]] = []
        if session_id is not None:
            self.use_session(session_id)

    def run_task(
        self,
        task: str,
        *,
        mode: RunMode,
        on_event: EventHandler | None = None,
        interrupt: InterruptHandler | None = None,
        steering: SteeringHandler | None = None,
        cancel_requested: CancellationHandler | None = None,
    ) -> RunState:
        prepared = self._prepare(task)
        state = self._run_single_turn(
            prepared,
            mode=mode,
            on_event=on_event,
            interrupt=interrupt,
            steering=steering,
            cancel_requested=cancel_requested,
        )
        handoff = state.handoff
        if handoff is None:
            return state
        if handoff.new_session:
            self._start_isolated_handoff_session(handoff)
        follow_up = self._run_single_turn(
            handoff.task,
            mode=handoff.mode,
            on_event=on_event,
            interrupt=interrupt,
            steering=steering,
            cancel_requested=cancel_requested,
        )
        if follow_up.handoff is not None:
            raise RuntimeError("Nested run handoffs are not supported.")
        return follow_up

    def _start_isolated_handoff_session(self, handoff: RunHandoff) -> None:
        if self.runtime is None:
            raise RuntimeError("Cannot start a handoff without an active runtime.")
        source_runtime = self.runtime
        proposal = (source_runtime.run.final_answer or "").strip()
        if not proposal:
            raise RuntimeError("Cannot start an isolated handoff without a completed plan proposal.")
        plan_message = AssistantMessage(content=proposal)

        if self.session_store is None:
            self.runtime = self.runner.empty_runtime(
                session_id=new_session_id(),
                messages=[plan_message],
            )
            self.conversation = text_messages(self.runtime.state.messages)
            return

        if self.active_session is None:
            raise RuntimeError("Cannot create an isolated handoff session without an active session.")
        source_session = self.active_session
        isolated_session = self.session_store.create_session(f"Implement: {source_session.title}")
        isolated_runtime = self.runner.empty_runtime(
            session_id=isolated_session.session_id,
            messages=[plan_message],
            runtime_store=self.session_store,
        )
        isolated_runtime.save()

        self.active_session = isolated_session
        self.runtime = isolated_runtime
        self.conversation = text_messages(isolated_runtime.state.messages)
        self._clear_pending_session()

    def _run_single_turn(
        self,
        prepared: str,
        *,
        mode: RunMode,
        on_event: EventHandler | None,
        interrupt: InterruptHandler | None,
        steering: SteeringHandler | None,
        cancel_requested: CancellationHandler | None,
    ) -> RunState:
        if self.session_store is not None:
            session = self.ensure_session(prepared)
            self._ensure_runtime(session.session_id)
            assert self.runtime is not None
            if self.runtime.state.status == "running":
                raise RuntimeError("The active session already has a running turn; resume or terminate it first.")
            run_id = new_run_id()
            self.session_store.start_turn(session.session_id, run_id, prepared)
        else:
            if self.runtime is None:
                self.runtime = self.runner.empty_runtime(session_id=new_session_id())
            if self.runtime.state.status == "running":
                raise RuntimeError("The active session already has a running turn; resume or terminate it first.")
            run_id = new_run_id()
        assert self.runtime is not None
        self.runtime.state.messages.append(UserMessage(content=prepared))
        self.runtime.state.current_run = RunState(
            task=prepared,
            mode=mode,
            run_id=run_id,
            history=self.runtime.state.messages,
        )
        self.runtime.state.active_message = None
        self.runtime.state.active_tool_index = None
        self.runtime.state.turn_usage = None
        self.runtime.state.status = "running"
        self.runtime.services.runtime_store = self.session_store
        self.runtime.services.on_event = on_event
        self.runtime.services.interrupt = interrupt
        self.runtime.services.steering = steering
        self.runtime.services.cancel_requested = cancel_requested
        runtime = self.runner.bind(self.runtime)
        try:
            state = self.runner.run(runtime)
        except Exception as exc:
            self._record_unexpected_failure(exc)
            raise
        if self.session_store is not None and self.active_session is not None:
            self.session_store.finish_turn(
                self.active_session.session_id,
                state.run_id,
                state.status,
                state.final_answer,
            )
            self._reload_active_session()
        self.conversation = text_messages(runtime.state.messages)
        return state

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
        """Return the display title for an unsaved session, if one is pending."""

        if not self._pending_session:
            return None
        value = " ".join((self._pending_title or "").split())
        return value[:80] or DEFAULT_SESSION_TITLE

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

    def history(self) -> list[dict[str, str]]:
        if self.runtime is None:
            return []
        return text_messages(self.runtime.state.messages)

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
        runtime = self.runner.empty_runtime(
            session_id=session.session_id,
            runtime_store=self.session_store,
        )
        runtime.save()
        self.active_session = session
        self.runtime = runtime
        self.conversation = []
        self._clear_pending_session()
        return session

    def _clear_pending_session(self) -> None:
        self._pending_session = False
        self._pending_title = None

    def _prepare(self, task: str) -> str:
        if self._task_preprocessor is None:
            return task
        try:
            return self._task_preprocessor.expand(task)
        except ToolError as exc:
            raise TaskPreparationError(str(exc)) from exc

    def _record_unexpected_failure(self, error: Exception) -> None:
        if self.runtime is None or self.runtime.state.current_run is None:
            return
        run = self.runtime.state.current_run
        run.status = "failed"
        run.final_answer = f"Unexpected runtime failure: {error}"
        run.add_event("error", run.final_answer, error_type=error.__class__.__name__)
        self.runtime.state.status = "idle"
        self.runtime.state.usage = self.runtime.state.turn_usage
        self.runtime.state.turn_usage = None
        publish = self.runtime.services.publish
        if publish is not None:
            publish(RuntimeEvent("thinking_end", data={"interrupted": True}))
            publish(
                RuntimeEvent(
                    "error",
                    run.final_answer,
                    {"error_type": error.__class__.__name__, "unexpected": True},
                )
            )
            run.add_event("run_finished", "Run finished", status=run.status)
            publish(RuntimeEvent("run_finished", run.status, {"final_answer": run.final_answer}))
        self.runtime.save()
        if self.session_store is not None and self.active_session is not None:
            self.session_store.finish_turn(
                self.active_session.session_id,
                run.run_id,
                run.status,
                run.final_answer,
            )

    def _reload_active_session(self) -> None:
        assert self.session_store is not None and self.active_session is not None
        session_id = self.active_session.session_id
        self.active_session = self.session_store.get_session(session_id)
