"""Todo-aware finalization policy for Agent Turns."""

from __future__ import annotations

from backend.domain import AssistantMessage, TodoSnapshot, UserMessage

from ..core.context import AgentRuntime
from ..core.events import RuntimeEvent

TODO_FINALIZATION_INSTRUCTION = (
    "The current Turn still has unfinished Todo items. This is the single finalization pass: update the Todo list "
    "to reflect work actually completed, then give the final answer. Do not mark unfinished work as completed."
)


def should_defer_todo_content(runtime: AgentRuntime) -> bool:
    """Keep the first candidate final answer private until its tool intent is known."""

    store = runtime.services.todo_store
    turn_id = runtime.run.turn_id
    if store is None or not turn_id:
        return False
    session_id = runtime.state.session_id
    return bool(store.snapshot(session_id, turn_id).unfinished and not store.finalization_claimed(session_id, turn_id))


def refresh_todo_finalization_context(runtime: AgentRuntime) -> None:
    """Restore the private correction instruction across loops and resumes."""

    store = runtime.services.todo_store
    turn_id = runtime.run.turn_id
    if store is None or not turn_id:
        runtime.services.context_suffix_messages = []
        return
    snapshot = store.snapshot(runtime.state.session_id, turn_id)
    if snapshot.unfinished and store.finalization_claimed(runtime.state.session_id, turn_id):
        runtime.services.context_suffix_messages = [UserMessage(content=TODO_FINALIZATION_INSTRUCTION)]
    else:
        runtime.services.context_suffix_messages = []


def check_todo_finalization(
    runtime: AgentRuntime,
    message: AssistantMessage,
    *,
    content_streamed: bool,
) -> bool:
    """Return true when one extra model pass must run before completion."""

    store = runtime.services.todo_store
    turn_id = runtime.run.turn_id
    if store is None or not turn_id:
        return False
    session_id = runtime.state.session_id
    snapshot = store.snapshot(session_id, turn_id)
    if not snapshot.unfinished:
        runtime.services.context_suffix_messages = []
        return False
    if store.claim_finalization(session_id, turn_id):
        runtime.services.context_suffix_messages = [UserMessage(content=TODO_FINALIZATION_INSTRUCTION)]
        return True

    snapshot = store.snapshot(session_id, turn_id)
    if snapshot.unfinished:
        disclosure = _unfinished_disclosure(snapshot)
        message.content = f"{message.content or ''}{disclosure}"
        if content_streamed:
            publish = runtime.services.publish or (lambda _event: None)
            publish(RuntimeEvent("response_start"))
            publish(RuntimeEvent("response_delta", disclosure))
            publish(RuntimeEvent("response_end"))
    runtime.services.context_suffix_messages = []
    return False


def _unfinished_disclosure(snapshot: TodoSnapshot) -> str:
    lines = ["", "", "Unfinished Todo items:"]
    lines.extend(f"- {todo.id} [{todo.status}] {todo.content}" for todo in snapshot.unfinished)
    return "\n".join(lines)


__all__ = [
    "TODO_FINALIZATION_INSTRUCTION",
    "check_todo_finalization",
    "refresh_todo_finalization_context",
    "should_defer_todo_content",
]
