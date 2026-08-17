"""Adapt neutral Job notifications to safe RuntimeEvent values."""

from __future__ import annotations

from collections.abc import Callable

from backend.jobs import JobStateChange

from .core.events import RuntimeEvent

_STATE_KINDS = {
    "pending": "job_queued",
    "running": "job_started",
    "succeeded": "job_succeeded",
    "failed": "job_failed",
    "cancelled": "job_cancelled",
}


class RuntimeJobEventBridge:
    """Publish safe presentation fields from a Job change."""

    def __init__(self, publish: Callable[[RuntimeEvent], None]) -> None:
        self._publish = publish

    def on_job_state_change(self, change: JobStateChange) -> None:
        kind = _STATE_KINDS.get(change.job_info.state.value)
        if change.reason == "service_degraded":
            kind = "job_degraded"
        if kind is None:
            return
        data: dict[str, object] = {
            "job_id": change.job_info.id,
            "job_kind": change.job_info.kind.value,
            "state": change.job_info.state.value,
        }
        if change.job_info.started_at is not None:
            data["started_at"] = change.job_info.started_at.isoformat()
        if change.job_info.finished_at is not None:
            data["finished_at"] = change.job_info.finished_at.isoformat()
        if change.job_info.cancel_requested_at is not None:
            data["cancel_requested"] = True
        if change.job_info.error:
            data["error"] = change.job_info.error
        self._publish(RuntimeEvent(kind, change.reason, data))  # type: ignore[arg-type]


__all__ = ["RuntimeJobEventBridge"]
