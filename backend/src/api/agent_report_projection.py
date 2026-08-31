"""Read-only API projection for Assistant report delivery metadata."""

from __future__ import annotations

from backend.domain.runtime_state import NodeFrame, RuntimeState


def _delivery_ids(turn: RuntimeState) -> set[str]:
    return {
        str(item["delivery_id"])
        for version in turn.data
        for message in version
        for item in message.get("content", [])
        if item.get("type") == "subagent"
        and item.get("event") == "agent_report"
        and isinstance(item.get("delivery_id"), str)
        and item["delivery_id"]
    }


def report_statuses(store: object, turn: RuntimeState) -> dict[str, str]:
    lookup = getattr(store, "agent_report_statuses", None)
    return dict(lookup(turn.session_id, _delivery_ids(turn))) if callable(lookup) else {}


def project_turn(store: object, turn: RuntimeState) -> dict[str, object]:
    payload = turn.to_dict()
    statuses = report_statuses(store, turn)
    if statuses:
        payload["agent_report_statuses"] = statuses
    return payload


def project_frame(store: object, frame: NodeFrame, current: RuntimeState) -> dict[str, object]:
    payload = frame.to_dict()
    statuses = report_statuses(store, current)
    if not statuses:
        return payload
    if frame.type == "turn.snapshot":
        turn = payload.get("turn")
        if isinstance(turn, dict):
            turn["agent_report_statuses"] = statuses
    else:
        payload["agent_report_statuses"] = statuses
    return payload


__all__ = ["project_frame", "project_turn", "report_statuses"]
