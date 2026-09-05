"""Agent-tree navigation, parent-mediated messages, and Thread SSE."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.domain import MessageQueueUnavailable
from backend.tools import ToolError

from ..runtime_event_transport import thread_sse
from ..session_files.routes import _store_for as session_file_store
from ..session_files.store import SessionFileError
from ..session_store import require_active_session, session_store
from ..state import WebAppState
from .turns import TurnExecutionConfig

router = APIRouter(prefix="/api/agent-threads", tags=["agent-threads"])


class AgentFileReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: Literal["project", "upload", "workspace"]
    path: str = Field(min_length=1, max_length=4000)
    display_path: str = Field(min_length=1, max_length=4000)


class AgentMessageRequest(TurnExecutionConfig):
    model_config = ConfigDict(extra="forbid")

    session_id: str = Field(min_length=1, max_length=200)
    content: str = Field(default="", max_length=20_000)
    references: list[AgentFileReference] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def validate_content(self):
        if not self.content.strip() and not self.references:
            raise ValueError("message requires content or references")
        return self


def _coordinator(state: WebAppState):
    return state.subagent_coordinator


@router.get("/{thread_id}/children")
def list_agent_thread_children(thread_id: str, session_id: str, request: Request) -> list[dict[str, str]]:
    store = session_store(request.app.state.web)
    require_active_session(store, session_id)
    try:
        return _coordinator(request.app.state.web).list_children(session_id, thread_id)
    except ToolError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/{target_thread_id}/messages", status_code=202)
def send_agent_thread_message(
    target_thread_id: str,
    body: AgentMessageRequest,
    request: Request,
) -> dict[str, object]:
    state: WebAppState = request.app.state.web
    store = session_store(state)
    require_active_session(store, body.session_id)
    runtime_config = {
        "provider_name": body.provider_name,
        "model": body.model.model_dump() if body.model is not None else None,
        "permission_mode": body.permission_mode,
        "running_mode": body.running_mode,
    }
    try:
        references = session_file_store(state, body.session_id).normalize_references(
            [item.model_dump() for item in body.references]
        )
        return _coordinator(state).send_from_root(
            body.session_id,
            target_thread_id,
            body.content,
            references=references,
            runtime_config={key: value for key, value in runtime_config.items() if value is not None},
        )
    except SessionFileError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except MessageQueueUnavailable as exc:
        raise HTTPException(status_code=503, detail="message_queue_unavailable") from exc
    except ToolError as exc:
        detail = str(exc)
        status = 404 if "tree" in detail.lower() else 422
        raise HTTPException(status_code=status, detail=detail) from exc


@router.get("/{thread_id}/stream")
def stream_agent_thread(thread_id: str, session_id: str, request: Request) -> StreamingResponse:
    state: WebAppState = request.app.state.web
    store = session_store(state)
    require_active_session(store, session_id)
    node = store.get_thread_node(session_id, thread_id)
    if node is None or node.session_id != session_id:
        raise HTTPException(status_code=404, detail="未知 Agent Thread。")
    return StreamingResponse(
        thread_sse(state, session_id, thread_id, request.headers.get("last-event-id")),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


__all__ = ["router"]
