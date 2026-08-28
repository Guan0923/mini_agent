"""Serialization boundary for provider-neutral run state."""

from __future__ import annotations

from dataclasses import asdict
from typing import TYPE_CHECKING, Any

from .messages import ToolMessage, message_to_dict, messages_from_dicts, tool_message_from_dict, tool_message_to_dict
from .skills import SkillSnapshot

if TYPE_CHECKING:
    from .state import RunState


def run_state_to_dict(state: RunState) -> dict[str, Any]:
    """Encode a run without coupling its model to a storage adapter."""

    return {
        "task": state.task,
        "mode": state.mode,
        "run_id": state.run_id,
        "thread_id": state.thread_id,
        "turn_id": state.turn_id,
        "data_idx": state.data_idx,
        "turn_start_index": state.turn_start_index,
        "history": [message_to_dict(message) for message in state.history],
        "actions": [tool_message_to_dict(action) for action in state.actions],
        "completed_steps": state.completed_steps,
        "final_answer": state.final_answer,
        "model_turns": state.model_turns,
        "skill_selection_calls": state.skill_selection_calls,
        "model_calls": state.model_calls,
        "tool_calls": state.tool_calls,
        "retries": state.retries,
        "status": state.status,
        "stop_reason": state.stop_reason,
        "handoff": asdict(state.handoff) if state.handoff else None,
        "active_skills": [skill.to_dict() for skill in state.active_skills],
        "provenance": asdict(state.provenance),
        "checkpoint": asdict(state.checkpoint) if state.checkpoint else None,
        "subagent_batches": state.subagent_batches,
    }


def run_state_from_dict(data: dict[str, Any]) -> RunState:
    """Decode current and legacy payloads into a normalized run model."""

    from .state import RecoveryCheckpoint, RunHandoff, RunProvenance, RunState, utc_now

    def tool(value: dict[str, Any], fallback_call_id: str) -> ToolMessage:
        if "type" in value:
            content = value.get("answer") if value.get("type") == "final_answer" else None
            return ToolMessage(
                name=str(value.get("tool") or "legacy"),
                call_id=fallback_call_id,
                arguments=dict(value.get("arguments") or {}),
                content=content if isinstance(content, str) else None,
                status="succeeded" if isinstance(content, str) else "pending",
            )
        return tool_message_from_dict(value, fallback_call_id=fallback_call_id)

    handoff_data = data.get("handoff")
    handoff = None
    if isinstance(handoff_data, dict):
        handoff = RunHandoff(
            mode=handoff_data.get("mode", "agent"),
            task=str(handoff_data.get("task") or ""),
            compact_before=(
                handoff_data["compact_before"] if isinstance(handoff_data.get("compact_before"), bool) else False
            ),
            active_skills=tuple(
                SkillSnapshot.from_dict(dict(item))
                for item in handoff_data.get("active_skills", [])
                if isinstance(item, dict)
            ),
        )

    provenance_data = data.get("provenance")
    if isinstance(provenance_data, dict):
        provenance = RunProvenance(
            workflow_id=str(provenance_data.get("workflow_id") or data["run_id"]),
            attempt=max(1, int(provenance_data.get("attempt", 1))),
            trigger=provenance_data.get("trigger", "legacy"),
            workspace_root=str(provenance_data["workspace_root"])
            if provenance_data.get("workspace_root") is not None
            else None,
            source_session_id=str(provenance_data["source_session_id"])
            if provenance_data.get("source_session_id") is not None
            else None,
            source_run_id=str(provenance_data["source_run_id"])
            if provenance_data.get("source_run_id") is not None
            else None,
        )
    else:
        provenance = RunProvenance(workflow_id=str(data["run_id"]), trigger="legacy")

    checkpoint_data = data.get("checkpoint")
    checkpoint = None
    if isinstance(checkpoint_data, dict) and checkpoint_data.get("reason"):
        checkpoint = RecoveryCheckpoint(
            reason=str(checkpoint_data["reason"]),
            timestamp=str(checkpoint_data.get("timestamp") or utc_now()),
            call_id=str(checkpoint_data["call_id"]) if checkpoint_data.get("call_id") else None,
            exchange_id=str(checkpoint_data["exchange_id"]) if checkpoint_data.get("exchange_id") else None,
            interruption=str(checkpoint_data["interruption"]) if checkpoint_data.get("interruption") else None,
            indeterminate_call_ids=tuple(str(item) for item in checkpoint_data.get("indeterminate_call_ids", [])),
        )

    raw_status = str(data.get("status") or "running")
    legacy_statuses = {
        "suspended": ("cancelled", "user_paused"),
        "interrupted": ("failed", "process_interrupted"),
        "terminated": ("cancelled", "user_terminated"),
    }
    status, legacy_reason = legacy_statuses.get(raw_status, (raw_status, None))
    if status not in {"running", "completed", "failed", "cancelled"}:
        status, legacy_reason = "failed", "execution_failed"
    raw_stop_reason = data.get("stop_reason")
    stop_reason = raw_stop_reason if isinstance(raw_stop_reason, str) else legacy_reason

    return RunState(
        task=data["task"],
        mode=data["mode"],
        run_id=data["run_id"],
        thread_id=str(data.get("thread_id") or ""),
        turn_id=str(data.get("turn_id") or ""),
        data_idx=max(0, int(data.get("data_idx", 0))),
        turn_start_index=int(data.get("turn_start_index", 0)),
        history=messages_from_dicts([dict(item) for item in data.get("history", [])]),
        actions=[
            tool(dict(item), f"call_legacy_action_{index}") for index, item in enumerate(data.get("actions", []), 1)
        ],
        completed_steps=list(data.get("completed_steps", [])),
        final_answer=data.get("final_answer"),
        model_turns=int(data.get("model_turns", 0)),
        skill_selection_calls=int(data.get("skill_selection_calls", 0)),
        model_calls=max(0, int(data.get("model_calls", 0))),
        tool_calls=max(0, int(data.get("tool_calls", 0))),
        retries=max(0, int(data.get("retries", 0))),
        status=status,  # type: ignore[arg-type]
        stop_reason=stop_reason,  # type: ignore[arg-type]
        active_skills=[
            SkillSnapshot.from_dict(dict(item)) for item in data.get("active_skills", []) if isinstance(item, dict)
        ],
        handoff=handoff,
        provenance=provenance,
        checkpoint=checkpoint,
        subagent_batches={
            str(key): dict(value)
            for key, value in (data.get("subagent_batches") or {}).items()
            if isinstance(value, dict)
        },
    )
