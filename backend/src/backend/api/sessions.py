"""Session management endpoints for the backend service."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from backend.domain import DEFAULT_TIME_ZONE, TIME_ZONE_OPTIONS
from backend.runtime import RunnerSettings, build_application

from .state import WebAppState

router = APIRouter(prefix="/api")


class SessionCreateBody(BaseModel):
    title: str | None = None


class TimezoneBody(BaseModel):
    timezone: str


def _store(state: WebAppState):
    from backend.configuration import load_config, section
    from backend.storage.sqlite import SQLiteSessionStore

    config = load_config(state.paths.config_file)
    device_id = str(section(config, "sync")["device_id"])
    return SQLiteSessionStore(state.paths, device_id)


def _summary(summary) -> dict:
    return {
        "session_id": summary.session_id,
        "title": summary.title,
        "created_at": summary.created_at,
        "updated_at": summary.updated_at,
        "message_count": summary.message_count,
        "last_run_id": summary.last_run_id,
        "last_run_status": summary.last_run_status,
    }


def _require_session(state: WebAppState, session_id: str):
    store = _store(state)
    summary = store.get_session_summary(session_id)
    if summary is None:
        raise HTTPException(status_code=404, detail=f"未知会话：{session_id}")
    return store, summary


@router.get("/sessions")
def list_sessions(request: Request) -> list[dict]:
    state: WebAppState = request.app.state.web
    store = _store(state)
    return [_summary(summary) for summary in store.list_sessions()]


@router.post("/sessions")
def create_session(body: SessionCreateBody, request: Request) -> dict:
    state: WebAppState = request.app.state.web
    store = _store(state)
    session = store.create_session(body.title)
    summary = store.get_session_summary(session.session_id)
    assert summary is not None
    return _summary(summary)


@router.get("/sessions/{session_id}")
def get_session(session_id: str, request: Request) -> dict:
    state: WebAppState = request.app.state.web
    _, summary = _require_session(state, session_id)
    return _summary(summary)


@router.get("/sessions/{session_id}/messages")
def get_session_messages(session_id: str, request: Request) -> list[dict]:
    state: WebAppState = request.app.state.web
    store = _store(state)
    if store.get_session_summary(session_id) is None:
        raise HTTPException(status_code=404, detail=f"未知会话：{session_id}")
    return store.load_conversation(session_id)


@router.get("/sessions/{session_id}/timezone")
def get_timezone(session_id: str, request: Request) -> dict:
    state: WebAppState = request.app.state.web
    _store_instance, _summary_value = _require_session(state, session_id)
    runtime = _store_instance.load_runtime(session_id)
    selected = runtime.state.timezone if runtime is not None else DEFAULT_TIME_ZONE
    return {
        "timezone": selected,
        "options": [{"identifier": option.identifier, "label": option.label} for option in TIME_ZONE_OPTIONS],
    }


@router.put("/sessions/{session_id}/timezone")
def set_timezone(session_id: str, body: TimezoneBody, request: Request) -> dict:
    state: WebAppState = request.app.state.web
    _require_session(state, session_id)
    application = None
    try:
        application = build_application(
            state.chat_workspace,
            planner_name="llm",
            settings=RunnerSettings(log_full_messages=True),
            project_mcp_enabled=False,
        )
        conversation = application.open_conversation(session_id)
        selected = conversation.set_timezone(body.timezone)
        return {"timezone": selected}
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    finally:
        if application is not None:
            application.close()


@router.post("/sessions/{session_id}/compact")
def compact_session(session_id: str, request: Request) -> dict:
    state: WebAppState = request.app.state.web
    _require_session(state, session_id)
    application = None
    try:
        application = build_application(
            state.chat_workspace,
            planner_name="llm",
            settings=RunnerSettings(log_full_messages=True),
            project_mcp_enabled=False,
        )
        conversation = application.open_conversation(session_id)
        result = conversation.compact_context()
        return {
            "compacted": result.compacted,
            "previous_messages": result.previous_messages,
            "remaining_messages": result.remaining_messages,
            "summary": result.summary,
        }
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    finally:
        if application is not None:
            application.close()


@router.get("/sessions/{session_id}/trace")
def get_trace(session_id: str, request: Request) -> dict:
    state: WebAppState = request.app.state.web
    store, summary = _require_session(state, session_id)
    runtime = store.load_runtime(session_id)
    current_run = runtime.state.current_run if runtime is not None else None
    return {
        "session_id": session_id,
        "title": summary.title,
        "run": current_run.to_dict() if current_run is not None else None,
    }


@router.get("/forkable-runs")
def list_forkable_runs(request: Request) -> list[dict[str, str]]:
    state: WebAppState = request.app.state.web
    return _store(state).list_forkable_runs()


@router.post("/runs/{run_id}/fork")
def fork_run(run_id: str, request: Request) -> dict:
    state: WebAppState = request.app.state.web
    try:
        session = _store(state).fork_run(run_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    summary = _store(state).get_session_summary(session.session_id)
    assert summary is not None
    return _summary(summary)
