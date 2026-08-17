"""Authenticated control-plane API for process-local Jobs."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from backend.jobs import TERMINAL_STATES, JobLane, JobQuery, JobState

from .auth.dependencies import require_user
from .auth.types import UserIdentity
from .state import WebAppState

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


def _registry(request: Request):
    state: WebAppState = request.app.state.web
    registry = getattr(state, "job_registry", None)
    if registry is None:
        raise HTTPException(status_code=503, detail="Job 控制面尚未初始化。")
    return registry


def _parse_query(state: str | None, lane: str | None) -> JobQuery | None:
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
    if states is None and lanes is None:
        return None
    return JobQuery(states=states, lanes=lanes)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _public(info: Any) -> dict[str, object]:
    job = info.info
    cancellable = job.state not in TERMINAL_STATES and job.cancel_requested_at is None
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
    }


@router.get("")
def list_jobs(
    request: Request,
    state: str | None = None,
    lane: str | None = None,
    session_id: str | None = None,
    identity: UserIdentity = Depends(require_user),
) -> list[dict[str, object]]:
    query = _parse_query(state, lane)
    registry = _registry(request)
    records = registry.list_for_user(identity.id, query, session_id=session_id)
    return [_public(item) for item in records]


@router.get("/{job_id}")
def get_job(job_id: str, request: Request, identity: UserIdentity = Depends(require_user)) -> dict[str, object]:
    record = _registry(request).get_for_user(identity.id, job_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Job 不存在。")
    return _public(record)


@router.post("/{job_id}/cancel", status_code=202)
def cancel_job(job_id: str, request: Request, identity: UserIdentity = Depends(require_user)) -> dict[str, object]:
    registry = _registry(request)
    record = registry.get_for_user(identity.id, job_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Job 不存在。")
    if record.info.state in TERMINAL_STATES:
        raise HTTPException(status_code=409, detail="Job 已经结束，无法取消。")
    if not registry.cancel_for_user(identity.id, job_id):
        refreshed = registry.get_for_user(identity.id, job_id)
        if refreshed is None:
            raise HTTPException(status_code=404, detail="Job 不存在。")
        if refreshed.info.state in TERMINAL_STATES:
            raise HTTPException(status_code=409, detail="Job 已经结束，无法取消。")
    refreshed = registry.get_for_user(identity.id, job_id)
    if refreshed is None:
        raise HTTPException(status_code=404, detail="Job 不存在。")
    return _public(refreshed)


__all__ = ["router"]
