"""CRUD API for user-visible SidebarThread metadata."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from backend.domain import (
    MessageQueueUnavailable,
    QueuedMessage,
    QueueItemConflict,
    QueueItemNotFound,
    QueueItemStateConflict,
)

from ..session_store import session_store
from ..state import WebAppState

router = APIRouter(prefix="/api/sidebar-threads", tags=["sidebar-threads"])


class CreateSidebarThreadRequest(BaseModel):
    title: str = Field(default="新对话", max_length=120)
    client_id: str | None = Field(default=None, max_length=200)


class RenameSidebarThreadRequest(BaseModel):
    title: str = Field(min_length=1, max_length=120)


class QueuedMessageBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str = Field(default="", max_length=200_000)
    references: list[dict[str, str]] = Field(default_factory=list, max_length=100)


class CreateQueuedMessageRequest(QueuedMessageBody):
    id: UUID


def _queue_error(exc: Exception) -> HTTPException:
    if isinstance(exc, MessageQueueUnavailable):
        return HTTPException(status_code=503, detail="message_queue_unavailable")
    if isinstance(exc, QueueItemNotFound):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, (QueueItemConflict, QueueItemStateConflict)):
        return HTTPException(status_code=409, detail=str(exc))
    return HTTPException(status_code=500, detail="message_queue_error")


def _queue_references(values: list[dict[str, str]]) -> tuple[dict[str, str], ...]:
    result: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for value in values:
        source, path = value.get("source"), value.get("path")
        if source not in {"project", "upload"} or not isinstance(path, str) or not path:
            raise HTTPException(status_code=422, detail="无效的文件引用。")
        key = (source, path)
        if key not in seen:
            seen.add(key)
            result.append({"source": source, "path": path})
    return tuple(result)


def _validate_queue_content(content: str, references: tuple[dict[str, str], ...]) -> str:
    normalized = content.strip()
    if not normalized and not references:
        raise HTTPException(status_code=422, detail="content 或文件引用至少需要一个。")
    return normalized


def _payload(store, item) -> dict[str, object]:
    return store.sidebar_thread_summary(item).to_dict()


def _require(store, thread_id: str):
    item = store.get_sidebar_thread(thread_id)
    if item is None:
        raise HTTPException(status_code=404, detail="未知 SidebarThread。")
    return item


def _require_queue_thread(store, thread_id: str):
    item = store.get_sidebar_thread(thread_id)
    if item is not None:
        return item
    for summary in store.list_sessions(state="all"):
        panel = store.active_right_panel_window_for_thread(summary.session_id, thread_id)
        if panel is not None:
            return panel
    raise HTTPException(status_code=404, detail="未知 Thread。")


@router.get("/{thread_id}/queued-messages")
def list_queued_messages(thread_id: str, request: Request) -> list[dict[str, object]]:
    _require_queue_thread(session_store(request.app.state.web), thread_id)
    try:
        return [item.to_dict() for item in request.app.state.web.message_queue.list(thread_id)]
    except Exception as exc:
        raise _queue_error(exc) from exc


@router.post("/{thread_id}/queued-messages", status_code=201)
def create_queued_message(
    thread_id: str, body: CreateQueuedMessageRequest, request: Request, response: Response
) -> dict[str, object]:
    _require_queue_thread(session_store(request.app.state.web), thread_id)
    references = _queue_references(body.references)
    item = QueuedMessage(str(body.id), thread_id, _validate_queue_content(body.content, references), references)
    try:
        stored, created = request.app.state.web.message_queue.create(item)
    except Exception as exc:
        raise _queue_error(exc) from exc
    response.status_code = 201 if created else 200
    return stored.to_dict()


@router.patch("/{thread_id}/queued-messages/{message_id}")
def update_queued_message(
    thread_id: str, message_id: str, body: QueuedMessageBody, request: Request
) -> dict[str, object]:
    _require_queue_thread(session_store(request.app.state.web), thread_id)
    references = _queue_references(body.references)
    try:
        item = request.app.state.web.message_queue.update(
            thread_id,
            message_id,
            content=_validate_queue_content(body.content, references),
            references=references,
        )
    except Exception as exc:
        raise _queue_error(exc) from exc
    return item.to_dict()


@router.delete("/{thread_id}/queued-messages/{message_id}", status_code=204)
def delete_queued_message(thread_id: str, message_id: str, request: Request) -> Response:
    _require_queue_thread(session_store(request.app.state.web), thread_id)
    try:
        request.app.state.web.message_queue.delete(thread_id, message_id)
    except Exception as exc:
        raise _queue_error(exc) from exc
    return Response(status_code=204)


@router.get("")
def list_sidebar_threads(
    request: Request,
    state: str = "active",
) -> list[dict[str, object]]:
    if state not in {"active", "archived", "deleted", "all"}:
        raise HTTPException(status_code=422, detail="无效的 SidebarThread 状态。")
    store = session_store(request.app.state.web)
    return [item.to_dict() for item in store.list_sidebar_thread_summaries(state=state)]


@router.post("", status_code=201)
def create_sidebar_thread(
    body: CreateSidebarThreadRequest,
    request: Request,
) -> dict[str, object]:
    state: WebAppState = request.app.state.web
    store = session_store(state)
    session = store.create_session(body.title, client_id=body.client_id)
    state.paths.ensure_session(session.session_id)
    item = store.create_sidebar_thread(
        session_id=session.session_id,
        thread_id=session.session_id,
        title=session.title,
        title_is_custom=session.title_is_custom,
    )
    return _payload(store, item)


@router.patch("/{thread_id}")
def rename_sidebar_thread(
    thread_id: str,
    body: RenameSidebarThreadRequest,
    request: Request,
) -> dict[str, object]:
    store = session_store(request.app.state.web)
    _require(store, thread_id)
    return _payload(
        store,
        store.update_sidebar_thread(thread_id, title=body.title.strip(), title_is_custom=True),
    )


@router.post("/{thread_id}/archive")
def archive_sidebar_thread(thread_id: str, request: Request) -> dict[str, object]:
    store = session_store(request.app.state.web)
    _require(store, thread_id)
    from backend.domain.state import utc_now

    return _payload(store, store.update_sidebar_thread(thread_id, archived_at=utc_now(), deleted_at=None))


@router.post("/{thread_id}/restore")
def restore_sidebar_thread(thread_id: str, request: Request) -> dict[str, object]:
    store = session_store(request.app.state.web)
    _require(store, thread_id)
    return _payload(store, store.update_sidebar_thread(thread_id, archived_at=None, deleted_at=None))


@router.delete("/{thread_id}")
def delete_sidebar_thread(thread_id: str, request: Request) -> dict[str, object]:
    store = session_store(request.app.state.web)
    _require(store, thread_id)
    from backend.domain.state import utc_now

    return _payload(store, store.update_sidebar_thread(thread_id, deleted_at=utc_now()))


__all__ = ["router"]
