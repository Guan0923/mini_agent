"""Authenticated local API for cloud snapshot preferences and jobs."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, StrictBool

from .auth.dependencies import require_user
from .auth.types import UserIdentity
from .state import WebAppState

router = APIRouter(prefix="/api/sync", tags=["sync"])


class SyncPreferencesBody(BaseModel):
    auto_save_enabled: StrictBool
    auto_save_rule: Literal["idle_5m", "after_run", "hourly"]


class SaveBody(BaseModel):
    force: bool = False


def _manager(state: WebAppState):
    if state.snapshot_manager is None:
        raise HTTPException(status_code=503, detail="云同步服务尚未配置。")
    return state.snapshot_manager


def _require_cloud_identity(identity: UserIdentity) -> None:
    if identity.is_guest:
        raise HTTPException(status_code=403, detail="游客数据仅保存在本机，登录正式账户后才能使用云同步。")


@router.get("/status")
def status(request: Request, identity: UserIdentity = Depends(require_user)) -> dict[str, object]:
    state: WebAppState = request.app.state.web
    if identity.is_guest:
        return {
            "available": False,
            "preferences": {"auto_save_enabled": False, "auto_save_rule": "idle_5m"},
            "state": {"status": "local_only", "local_revision": 0, "uploaded_revision": 0},
            "job": None,
        }
    if state.snapshot_manager is None:
        return {
            "available": False,
            "preferences": state.settings.sync_preferences_for_user(identity.id),
            "state": state.settings.sync_state_for_user(identity.id),
            "job": None,
        }
    return {
        "available": True,
        "preferences": state.settings.sync_preferences_for_user(identity.id),
        "state": state.settings.sync_state_for_user(identity.id),
        "job": _manager(state).active_job(identity.id),
    }


@router.put("/preferences")
def update_preferences(
    body: SyncPreferencesBody,
    request: Request,
    identity: UserIdentity = Depends(require_user),
) -> dict[str, object]:
    _require_cloud_identity(identity)
    try:
        return request.app.state.web.settings.update_sync_preferences(identity.id, body.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/save", status_code=202)
def save(
    body: SaveBody,
    request: Request,
    identity: UserIdentity = Depends(require_user),
) -> dict[str, object]:
    _require_cloud_identity(identity)
    state: WebAppState = request.app.state.web
    return _manager(state).start_save(identity.id, force=body.force)


@router.get("/jobs/{job_id}")
def get_job(
    job_id: str,
    request: Request,
    identity: UserIdentity = Depends(require_user),
) -> dict[str, object]:
    _require_cloud_identity(identity)
    state: WebAppState = request.app.state.web
    job = _manager(state).job(identity.id, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="同步任务不存在。")
    return job


@router.get("/snapshots")
def snapshots(request: Request, identity: UserIdentity = Depends(require_user)) -> list[dict[str, object]]:
    _require_cloud_identity(identity)
    state: WebAppState = request.app.state.web
    return _manager(state).snapshots(identity.id)


@router.post("/snapshots/{snapshot_id}/restore", status_code=202)
def restore(
    snapshot_id: str,
    request: Request,
    identity: UserIdentity = Depends(require_user),
) -> dict[str, object]:
    _require_cloud_identity(identity)
    state: WebAppState = request.app.state.web
    return _manager(state).start_restore(identity.id, snapshot_id)


@router.post("/conflicts/use-local", status_code=202)
def resolve_with_local(request: Request, identity: UserIdentity = Depends(require_user)) -> dict[str, object]:
    _require_cloud_identity(identity)
    state: WebAppState = request.app.state.web
    return _manager(state).start_save(identity.id, force=True)


@router.post("/conflicts/use-cloud", status_code=202)
def resolve_with_cloud(
    snapshot_id: str,
    request: Request,
    identity: UserIdentity = Depends(require_user),
) -> dict[str, object]:
    _require_cloud_identity(identity)
    state: WebAppState = request.app.state.web
    return _manager(state).start_restore(identity.id, snapshot_id)
