"""Session management endpoints for the backend service."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from .state import WebAppState

router = APIRouter(prefix="/api")


def _store(state: WebAppState):
    from backend.configuration import load_config, section
    from backend.storage.sqlite import SQLiteSessionStore

    config = load_config(state.paths.config_file)
    device_id = str(section(config, "sync")["device_id"])
    return SQLiteSessionStore(state.paths, device_id)


@router.get("/sessions")
def list_sessions(request: Request) -> list[dict]:
    state: WebAppState = request.app.state.web
    store = _store(state)
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
def get_session(session_id: str, request: Request) -> dict:
    state: WebAppState = request.app.state.web
    store = _store(state)
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
def get_session_messages(session_id: str, request: Request) -> list[dict]:
    state: WebAppState = request.app.state.web
    store = _store(state)
    if store.get_session_summary(session_id) is None:
        raise HTTPException(status_code=404, detail=f"未知会话：{session_id}")
    return store.load_conversation(session_id)
