"""Shared access to the local Session/Turn store."""

from __future__ import annotations

from fastapi import HTTPException

from .state import WebAppState


def session_store(state: WebAppState):
    from backend.storage.sqlite import SQLiteSessionStore

    return SQLiteSessionStore(state.paths, getattr(state, "agent_thread_index", None))


def require_session(store, session_id: str):
    try:
        summary = store.get_session_summary(session_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Session id 无效。") from exc
    if summary is None:
        raise HTTPException(status_code=404, detail=f"未知 Session：{session_id}")
    return summary


def require_active_session(store, session_id: str):
    summary = require_session(store, session_id)
    if summary.deleted_at is not None:
        raise HTTPException(status_code=409, detail="Session 已删除，无法继续操作。")
    if summary.archived_at is not None:
        raise HTTPException(status_code=409, detail="Session 已归档，请先恢复。")
    return summary


def mutation_error(exc: Exception) -> HTTPException:
    return HTTPException(status_code=409 if isinstance(exc, RuntimeError) else 400, detail=str(exc))


def summary_payload(state: WebAppState, summary) -> dict[str, object]:
    project = state.projects.session_project(summary.session_id)
    return {
        "session_id": summary.session_id,
        "title": summary.title,
        "created_at": summary.created_at,
        "updated_at": summary.updated_at,
        "conversation_updated_at": summary.updated_at,
        "message_count": summary.message_count,
        "last_node_id": summary.last_node_id,
        "last_run_id": summary.last_run_id,
        "last_run_status": summary.last_run_status,
        "client_id": summary.client_id,
        "archived_at": summary.archived_at,
        "deleted_at": summary.deleted_at,
        "title_is_custom": summary.title_is_custom,
        "project_id": project.project_id if project is not None else None,
        "project_available": project.available if project is not None else None,
    }


__all__ = ["mutation_error", "require_active_session", "require_session", "session_store", "summary_payload"]
