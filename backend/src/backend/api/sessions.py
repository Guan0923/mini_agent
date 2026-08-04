"""Session management endpoints for the backend service."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from .auth_dependencies import require_user
from .auth_types import UserIdentity
from .state import WebAppState

router = APIRouter(prefix="/api")


def _store(state: WebAppState, user_id: str):
    from backend.configuration import load_config, section
    from backend.storage.sqlite import SQLiteSessionStore

    paths = state.user_paths(user_id)
    config = load_config(paths.config_file)
    device_id = str(section(config, "sync")["device_id"])
    return SQLiteSessionStore(paths, device_id)


@router.get("/sessions")
def list_sessions(request: Request, identity: UserIdentity = Depends(require_user)) -> list[dict]:
    state: WebAppState = request.app.state.web
    store = _store(state, identity.id)
    return [
        {
            "session_id": summary.session_id,
            "title": summary.title,
            "created_at": summary.created_at,
            "updated_at": summary.updated_at,
            "message_count": summary.message_count,
            "last_run_status": summary.last_run_status,
        }
        for summary in store.list_sessions()
    ]


@router.get("/sessions/{session_id}")
def get_session(session_id: str, request: Request, identity: UserIdentity = Depends(require_user)) -> dict:
    state: WebAppState = request.app.state.web
    store = _store(state, identity.id)
    summary = store.get_session_summary(session_id)
    if summary is None:
        raise HTTPException(status_code=404, detail=f"未知会话：{session_id}")
    return {
        "session_id": summary.session_id,
        "title": summary.title,
        "created_at": summary.created_at,
        "updated_at": summary.updated_at,
        "message_count": summary.message_count,
        "last_run_status": summary.last_run_status,
    }


@router.get("/sessions/{session_id}/messages")
def get_session_messages(
    session_id: str, request: Request, identity: UserIdentity = Depends(require_user)
) -> list[dict]:
    state: WebAppState = request.app.state.web
    store = _store(state, identity.id)
    if store.get_session_summary(session_id) is None:
        raise HTTPException(status_code=404, detail=f"未知会话：{session_id}")
    return store.load_conversation(session_id)
