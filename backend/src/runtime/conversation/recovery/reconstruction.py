"""Pure reconstruction of durable workflow attempts after process loss."""

from __future__ import annotations

from backend.domain import (
    RecoveryCheckpoint,
    ResumePreview,
    RunState,
    Session,
    new_run_id,
)
from backend.domain.state import utc_now

from ...core.context import RunSummary, RuntimeState


def build_preview(session: Session, state: RuntimeState | None) -> ResumePreview:
    run = state.current_run if state is not None else None
    if run is None:
        return ResumePreview(
            session.session_id,
            session.title,
            state.workspace_root if state else None,
            None,
            None,
            None,
            None,
            None,
            None,
            "idle",
        )
    checkpoint = run.checkpoint
    called = {
        str(message.data.get("call_id"))
        for message in run.runtime_messages
        if message.kind == "tool_call" and message.data.get("call_id")
    }
    finished = {
        str(message.data.get("call_id"))
        for message in run.runtime_messages
        if message.kind in {"tool_result", "tool_failed"} and message.data.get("call_id")
    }
    uncertain = tuple(sorted(called - finished))
    process_interrupted = state.status == "running" and run.status == "running"
    status = "failed" if process_interrupted else run.status
    stop_reason = "process_interrupted" if process_interrupted else run.stop_reason
    return ResumePreview(
        session_id=session.session_id,
        title=session.title,
        workspace_root=run.provenance.workspace_root or state.workspace_root,
        workflow_id=run.provenance.workflow_id,
        run_id=run.run_id,
        attempt=run.provenance.attempt,
        task=run.task,
        mode=run.mode,
        status=status,
        stop_reason=stop_reason,
        source_session_id=run.provenance.source_session_id,
        source_run_id=run.provenance.source_run_id,
        checkpoint_reason=checkpoint.reason if checkpoint else None,
        checkpoint_at=checkpoint.timestamp if checkpoint else None,
        interruption_reason=stop_reason,
        indeterminate_call_ids=uncertain or (checkpoint.indeterminate_call_ids if checkpoint else ()),
    )


def reconstruct_attempt(state: RuntimeState) -> tuple[RuntimeState, RuntimeState]:
    """Return archived source and a safe new attempt without replaying pending tools."""

    if state.current_run is None or state.current_run.status not in {"running", "failed", "cancelled"}:
        raise RuntimeError("The selected session has no resumable workflow.")

    source = RuntimeState.from_dict(state.to_dict())
    resumed = RuntimeState.from_dict(state.to_dict())
    assert source.current_run is not None and resumed.current_run is not None
    source_run = source.current_run
    old_run = resumed.current_run

    interrupted_at = utc_now()
    process_interrupted = source_run.status == "running"
    interruption = "process_interrupted" if process_interrupted else source_run.stop_reason
    if source_run.status == "running":
        source_run.status = "failed"
        source_run.stop_reason = "process_interrupted"
        source.status = "idle"

    called = {
        str(message.data.get("call_id"))
        for message in old_run.runtime_messages
        if message.kind == "tool_call" and message.data.get("call_id")
    }
    finished = {
        str(message.data.get("call_id"))
        for message in old_run.runtime_messages
        if message.kind in {"tool_result", "tool_failed"} and message.data.get("call_id")
    }
    indeterminate: list[str] = []
    active = resumed.active_message
    if active is not None:
        for tool in active.tool_messages:
            if tool.status != "pending":
                continue
            if tool.call_id in called - finished:
                tool.status = "indeterminate"
                tool.content = (
                    "Outcome is indeterminate because the process stopped after the tool call began. "
                    "Do not replay this call automatically; inspect current state first."
                )
                indeterminate.append(tool.call_id)
            else:
                tool.status = "failed"
                tool.content = "Not executed before the previous process stopped."
                tool.retryable = False
        if not any(message == active for message in resumed.messages):
            resumed.messages.append(active)

        normalized = {tool.call_id: tool for tool in active.tool_messages}
        for action in old_run.actions:
            replacement = normalized.get(action.call_id)
            if replacement is not None:
                action.status = replacement.status
                action.content = replacement.content
                action.retryable = replacement.retryable

    source_active = source.active_message
    if source_active is not None and active is not None:
        resumed_tools = {tool.call_id: tool for tool in active.tool_messages}
        for tool in source_active.tool_messages:
            replacement = resumed_tools.get(tool.call_id)
            if replacement is not None:
                tool.status = replacement.status
                tool.content = replacement.content
                tool.retryable = replacement.retryable
        source_actions = {action.call_id: action for action in source_run.actions}
        for action in old_run.actions:
            archived = source_actions.get(action.call_id)
            if archived is not None:
                archived.status = action.status
                archived.content = action.content
                archived.retryable = action.retryable

    if process_interrupted:
        for run in (source_run, old_run):
            for batch in run.subagent_batches.values():
                if batch.get("status") == "running":
                    batch["status"] = "indeterminate"
                for task in batch.get("tasks", []):
                    if isinstance(task, dict) and task.get("status") in {"queued", "running"}:
                        task["status"] = "indeterminate"
        message = "Previous process stopped before the workflow reached a terminal state."
        source_run.final_answer = source_run.final_answer or message
        source_run.add_event("run_interrupted", message)
        source_run.add_runtime_message(
            "run_interrupted",
            message,
            timestamp=interrupted_at,
            data={
                "session_id": source.session_id,
                "run_id": source_run.run_id,
                "workflow_id": source_run.provenance.workflow_id,
                "workflow_attempt": source_run.provenance.attempt,
                "workflow_trigger": source_run.provenance.trigger,
                "workspace_root": source_run.provenance.workspace_root or source.workspace_root,
                "source_session_id": source_run.provenance.source_session_id,
                "source_run_id": source_run.provenance.source_run_id,
                "status": source_run.status,
            },
        )
    for call_id in indeterminate:
        message = "Tool outcome is indeterminate after process interruption."
        source_run.add_event("tool_indeterminate", message, call_id=call_id)
        source_run.add_runtime_message(
            "tool_indeterminate",
            message,
            timestamp=interrupted_at,
            data={
                "session_id": source.session_id,
                "run_id": source_run.run_id,
                "workflow_id": source_run.provenance.workflow_id,
                "workflow_attempt": source_run.provenance.attempt,
                "workflow_trigger": source_run.provenance.trigger,
                "workspace_root": source_run.provenance.workspace_root or source.workspace_root,
                "source_session_id": source_run.provenance.source_session_id,
                "source_run_id": source_run.provenance.source_run_id,
                "call_id": call_id,
                "status": "indeterminate",
            },
        )

    resumed.active_message = None
    resumed.active_tool_index = None
    source_run.checkpoint = RecoveryCheckpoint(
        reason=source_run.checkpoint.reason if source_run.checkpoint else "unknown",
        timestamp=source_run.checkpoint.timestamp if source_run.checkpoint else interrupted_at,
        call_id=source_run.checkpoint.call_id if source_run.checkpoint else None,
        exchange_id=source_run.checkpoint.exchange_id if source_run.checkpoint else None,
        interruption=interruption,
        indeterminate_call_ids=tuple(indeterminate),
    )

    payload = old_run.to_dict()
    new_id = new_run_id()
    payload.update(
        run_id=new_id,
        status="running",
        stop_reason=None,
        final_answer=None,
        handoff=None,
        events=[],
        runtime_messages=[],
        provenance={
            "workflow_id": old_run.provenance.workflow_id,
            "attempt": old_run.provenance.attempt + 1,
            "trigger": "resume",
            "workspace_root": old_run.provenance.workspace_root or resumed.workspace_root,
            "source_session_id": resumed.session_id,
            "source_run_id": old_run.run_id,
        },
        checkpoint={
            "reason": "run_resumed",
            "timestamp": interrupted_at,
            "call_id": None,
            "exchange_id": None,
            "interruption": None,
            "indeterminate_call_ids": [],
        },
    )
    new_run = RunState.from_dict(payload)
    new_run.history = resumed.messages
    resumed.current_run = new_run
    resumed.status = "running"
    resumed.run_history.append(
        RunSummary(
            source_run.run_id,
            source_run.task,
            source_run.status,
            source_run.mode,
            source_run.final_answer,
            source_run.provenance.workflow_id,
            source_run.provenance.attempt,
        )
    )
    return source, resumed
