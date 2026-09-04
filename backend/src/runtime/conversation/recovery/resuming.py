"""Application workflow for selecting and resuming durable attempts."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from backend.domain import ResumePreview, RunState, Session
from backend.domain.runtime_state import RuntimeState as RuntimeTreeState

from ...core.context import AgentRuntime, text_messages
from ...core.contracts import (
    CancellationHandler,
    EventHandler,
    InterruptDecision,
    InterruptHandler,
    InterruptRequest,
    SteeringHandler,
)
from ...execution import RuntimeRunner
from ..ports import SessionStore
from .reconstruction import build_preview, reconstruct_attempt


class ResumableConversation(Protocol):
    runner: RuntimeRunner
    session_store: SessionStore | None
    active_session: Session | None
    runtime: AgentRuntime | None
    conversation: list[dict[str, str]]

    def use_session(self, session_id: str) -> Session: ...

    def _clear_pending_session(self) -> None: ...

    def _record_unexpected_failure(self, error: Exception) -> None: ...

    def _reload_active_session(self) -> None: ...

    def _bind_resume_node_bridge(self, turn_id: str, on_event: EventHandler | None): ...

    def _finish_resume_node_bridge(self, state: RunState) -> None: ...

    def _fail_resume_node_bridge(self, bridge, error: Exception) -> None: ...


def prepare_resume(conversation: ResumableConversation, session_id: str | None = None) -> ResumePreview:
    """Inspect a resume target without changing the active conversation."""

    store = conversation.session_store
    if store is None:
        raise RuntimeError("Session storage is not configured.")
    session = store.get_session(session_id) if session_id else conversation.active_session
    if session is None and session_id is None:
        session = store.latest_session()
    if session is None:
        if session_id:
            raise ValueError(f"Unknown session: {session_id}")
        raise ValueError("No saved session is available to resume.")
    state = store.load_runtime(session.session_id)
    run = state.current_run if state is not None else None
    finder = getattr(store, "find_node", None)
    turn = finder(run.turn_id) if run is not None and run.turn_id and callable(finder) else None
    return build_preview(session, state, turn if isinstance(turn, RuntimeTreeState) else None)


def resume_session(
    conversation: ResumableConversation,
    session_id: str | None = None,
    *,
    on_event: EventHandler | None = None,
    interrupt: InterruptHandler | None = None,
    steering: SteeringHandler | None = None,
    cancel_requested: CancellationHandler | None = None,
    suspend_requested: CancellationHandler | None = None,
    request_parameters: Mapping[str, Any] | None = None,
    resume_confirmed: bool = False,
) -> RunState | None:
    """Select an idle session or continue a stopped workflow as a new attempt."""

    preview = prepare_resume(conversation, session_id)
    if not preview.requires_action:
        conversation.use_session(preview.session_id)
        return None
    details = "\n".join(
        value
        for value in (
            f"SESSION {preview.session_id}",
            f"WORKSPACE {preview.workspace_root or 'unknown'}",
            f"WORKFLOW {preview.workflow_id or 'unknown'}",
            f"RUN {preview.run_id or 'unknown'} ATTEMPT {preview.attempt or 1}",
            f"TASK {preview.task or ''}",
            f"MODE {preview.mode or 'unknown'}",
            f"STATUS {preview.status}",
            f"STOP REASON {preview.stop_reason}" if preview.stop_reason else "",
            f"CHECKPOINT {preview.checkpoint_reason or 'unknown'} {preview.checkpoint_at or ''}".rstrip(),
            "INDETERMINATE " + ", ".join(preview.indeterminate_call_ids) if preview.indeterminate_call_ids else "",
        )
        if value
    )
    request = InterruptRequest(
        "resume",
        "Continue this durable workflow or go back?",
        {"details": details, "session_id": preview.session_id, "run_id": preview.run_id},
    )
    decision = (
        InterruptDecision("continue") if resume_confirmed else (interrupt(request) if interrupt is not None else None)
    )
    if decision is None or decision.choice == "back":
        return None
    if decision.choice != "continue":
        raise RuntimeError(f"Invalid resume decision: {decision.choice}")

    store = conversation.session_store
    assert store is not None
    state = store.load_runtime(preview.session_id)
    if state is None:
        raise RuntimeError("The selected session has no durable runtime state.")
    current_workspace = getattr(conversation.runner, "workspace_root", None)
    if state.workspace_root and current_workspace and state.workspace_root != current_workspace:
        raise RuntimeError(
            f"Workflow belongs to workspace {state.workspace_root}; current workspace is {current_workspace}."
        )
    current_project_cwd = getattr(conversation.runner, "project_cwd", None)
    if (state.project_cwd or None) != (current_project_cwd or None):
        raise RuntimeError(
            f"Workflow belongs to project workspace {state.project_cwd or 'none'}; "
            f"current project workspace is {current_project_cwd or 'none'}."
        )
    run = state.current_run
    turn = None
    if run is not None and run.turn_id:
        from .todo_receipts import reconcile_todo_receipts

        settle = getattr(store, "settle_indeterminate_tool_calls", None)
        finder = getattr(store, "find_node", None)
        turn = finder(run.turn_id) if callable(finder) else None
        if isinstance(turn, RuntimeTreeState):
            turn = reconcile_todo_receipts(store, getattr(conversation.runner, "todo_store", None), turn)
        if callable(settle):
            settle(run.turn_id)
    source, resumed = reconstruct_attempt(state, turn if isinstance(turn, RuntimeTreeState) else None)
    store.resume_runtime(source, resumed)
    session = store.get_session(preview.session_id)
    assert session is not None
    runtime = conversation.runner.empty_runtime(session_id=preview.session_id, runtime_store=store)
    runtime.state = resumed
    conversation.runtime = conversation.runner.bind(runtime)
    conversation.active_session = session
    conversation._clear_pending_session()
    conversation.runtime.services.on_event = on_event
    conversation.runtime.services.interrupt = interrupt
    conversation.runtime.services.steering = steering
    conversation.runtime.services.cancel_requested = cancel_requested
    conversation.runtime.services.suspend_requested = suspend_requested
    if resumed.current_run is None or not resumed.current_run.turn_id:
        raise RuntimeError("The resumed workflow is not bound to a canonical Turn.")
    bridge = conversation._bind_resume_node_bridge(resumed.current_run.turn_id, on_event)
    if request_parameters:
        conversation.runtime.state.request_parameters.update(dict(request_parameters))
    try:
        result = conversation.runner.resume(conversation.runtime)
    except Exception as exc:
        conversation._fail_resume_node_bridge(bridge, exc)
        conversation._record_unexpected_failure(exc)
        raise
    conversation._finish_resume_node_bridge(result)
    store.finish_turn(preview.session_id, result.run_id, result.status, result.final_answer)
    conversation.conversation = text_messages(conversation.runtime.state.messages)
    conversation._reload_active_session()
    return result
