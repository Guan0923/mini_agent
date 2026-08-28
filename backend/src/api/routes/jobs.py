"""Local control-plane API for process-local Jobs."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from backend.jobs import TERMINAL_STATES, JobLane, JobQuery, JobState
from backend.sandbox import SandboxLimits

from ..state import WebAppState

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


def _registry(request: Request):
    state: WebAppState = request.app.state.web
    registry = getattr(state, "job_registry", None)
    if registry is None:
        raise HTTPException(status_code=503, detail="Job 控制面尚未初始化。")
    return registry


def _parse_query(state: str | None, lane: str | None, session_id: str | None = None) -> JobQuery | None:
    states = None
    lanes = None
    if state is not None:
        try:
            states = (JobState(state),)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="无效的 Job 状态。") from exc
    if lane is not None:
        try:
            lanes = (JobLane(lane),)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="无效的 Job 资源池。") from exc
    if states is None and lanes is None and session_id is None:
        return None
    return JobQuery(states=states, lanes=lanes, session_id=session_id)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _public(info: Any) -> dict[str, object]:
    job = info.info
    cancellable = job.state not in TERMINAL_STATES and job.cancel_requested_at is None
    policy = getattr(job, "sandbox_policy", None)
    sandbox = getattr(job, "sandbox", None)
    if isinstance(sandbox, dict):
        sandbox_projection = dict(sandbox)
    elif policy is not None and callable(getattr(policy, "to_dict", None)):
        raw = policy.to_dict()
        sandbox_projection = {
            "enforced": bool(raw.get("enforced", True)),
            "file_mode": raw.get("file_mode", "read_only"),
            "network_mode": raw.get("network_mode", "no_network"),
            "limits": raw.get("limits", SandboxLimits().to_dict()),
        }
    else:
        sandbox_projection = {
            "enforced": False,
            "file_mode": "read_only",
            "network_mode": "no_network",
            "limits": SandboxLimits().to_dict(),
        }
    sandbox_projection.setdefault("failure_code", None)
    sandbox_projection.setdefault("cleanup_pending", False)
    return {
        "id": job.id,
        "kind": job.kind.value,
        "lane": info.lane.value,
        "state": job.state.value,
        "health": job.health,
        "started_at": _iso(job.started_at),
        "finished_at": _iso(job.finished_at),
        "queued_at": _iso(info.queued_at),
        "admitted_at": _iso(info.admitted_at),
        "error": job.error,
        "exit_code": job.exit_code,
        "cancel_requested": job.cancel_requested_at is not None,
        "cancellable": cancellable,
        "sandbox": sandbox_projection,
    }


@router.get("")
def list_jobs(
    request: Request,
    state: str | None = None,
    lane: str | None = None,
    session_id: str | None = None,
) -> list[dict[str, object]]:
    query = _parse_query(state, lane, session_id)
    registry = _registry(request)
    records = registry.root_scope().list(query)
    return [_public(item) for item in records]


@router.get("/{job_id}")
def get_job(job_id: str, request: Request) -> dict[str, object]:
    record = _registry(request).root_scope().get(job_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Job 不存在。")
    return _public(record)


@router.post("/{job_id}/cancel", status_code=202)
def cancel_job(job_id: str, request: Request) -> dict[str, object]:
    registry = _registry(request)
    scope = registry.root_scope()
    record = scope.get(job_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Job 不存在。")
    if record.info.state in TERMINAL_STATES:
        raise HTTPException(status_code=409, detail="Job 已经结束，无法取消。")
    if not scope.cancel(job_id):
        refreshed = scope.get(job_id)
        if refreshed is None:
            raise HTTPException(status_code=404, detail="Job 不存在。")
        if refreshed.info.state in TERMINAL_STATES:
            raise HTTPException(status_code=409, detail="Job 已经结束，无法取消。")
    refreshed = scope.get(job_id)
    if refreshed is None:
        raise HTTPException(status_code=404, detail="Job 不存在。")
    return _public(refreshed)


__all__ = ["router"]
