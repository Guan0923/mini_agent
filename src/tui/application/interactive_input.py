"""Submission handling for the interactive TUI run loop."""

from __future__ import annotations

from ..widgets import ChoiceItem
from .interactive_state import InteractiveHost, InteractiveRunState


def handle_submission(
    host: InteractiveHost,
    state: InteractiveRunState,
    submitted: str | None,
) -> bool:
    """Apply one submission and return whether the view should exit."""

    if submitted is None:
        if state.active_run is not None or state.approval.pending or state.active_compaction is not None:
            host._write("Agent is still running; finish the active review before exiting.")
        elif state.permission_pending:
            state.permission_pending = False
            state.deferred_task = None
            host._write("Permission selection cancelled.")
        else:
            return True
        return False

    task = submitted.strip()
    if task == "/quit":
        return _handle_quit(host, state)
    if state.exit_after_run or state.permission_pending:
        return False
    if state.active_run is not None or state.active_compaction is not None:
        _handle_busy_submission(host, state, task)
        return False

    parts = host._split_input(task)
    has_compact = any(kind == "command" and value == "compact" for kind, value, _argument in parts)
    if has_compact:
        if task != "/compact":
            host._write("Usage: /compact")
        else:
            state.view.begin_compaction()
            state.active_compaction = state.launch_compaction()
        return False

    keep_running, next_task, request_permission = host._handle_view_input(task)
    if request_permission:
        _begin_permission_review(host, state, next_task)
    elif next_task is not None:
        host._write_user_message(next_task)
        state.active_run = state.launch(next_task)

    pending_resume_id = getattr(host, "_pending_resume_id", ...)
    if pending_resume_id is not ...:
        host._pending_resume_id = ...
        state.active_run = state.launch_resume(pending_resume_id)
    return not keep_running


def _handle_quit(host: InteractiveHost, state: InteractiveRunState) -> bool:
    if state.active_run is not None or state.approval.pending or state.active_compaction is not None:
        if not state.exit_after_run:
            state.exit_after_run = True
            state.suspend_requested.set()
            host._drain_steering(state.pending_messages)
            state.approval.cancel_pending()
            host._write("SUSPENDING — waiting for a safe checkpoint")
        return False
    if state.permission_pending:
        state.permission_pending = False
        state.deferred_task = None
    host._write("Bye.")
    return True


def _handle_busy_submission(
    host: InteractiveHost,
    state: InteractiveRunState,
    task: str,
) -> None:
    if not task:
        return
    if task == "/history":
        host._show_history()
    elif any(kind == "command" for kind, _value, _argument in host._split_input(task)):
        host._write("Commands are unavailable while the agent is running.")
    else:
        state.pending_messages.put(task)
        host._write_queued_message(task)
        host._write("MESSAGE QUEUED")


def _begin_permission_review(
    host: InteractiveHost,
    state: InteractiveRunState,
    next_task: str | None,
) -> None:
    state.permission_pending = True
    state.deferred_task = next_task
    current = host._approval.permission_mode.replace("_", " ").title()
    state.view.begin_review(
        "PERMISSION",
        f"Current: {current}",
        "Choose how tools that require confirmation are handled.",
        (
            ChoiceItem(
                "approval_for_me",
                "Approval for me",
                "Ask before tools that require confirmation.",
            ),
            ChoiceItem(
                "full_access",
                "Full access",
                "Automatically approve tool calls.",
            ),
            ChoiceItem("cancel", "Cancel"),
        ),
        lambda choice, _supplement: state.permission_decisions.put_nowait(choice),
    )
