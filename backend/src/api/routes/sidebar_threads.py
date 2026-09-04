"""CRUD API for user-visible SidebarThread metadata."""

from __future__ import annotations

from typing import Any, Literal
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


class SidebarThreadOrderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str | None = Field(default=None, max_length=200)
    ordered_thread_ids: list[str] | None = Field(default=None, max_length=10_000)
    sort_by: Literal["created_at", "recent_activity"] | None = None


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


def _project_id(state: WebAppState, session_id: str) -> str | None:
    project = state.projects.session_project(session_id)
    return project.project_id if project is not None else None


def _summary_payload(summary, state: WebAppState) -> dict[str, object]:
    payload = summary.to_dict()
    project = state.projects.session_project(summary.thread.session_id)
    payload["project_id"] = project.project_id if project is not None else None
    payload["project_available"] = project.available if project is not None else None
    return payload


def _payload(store, item, state: WebAppState) -> dict[str, object]:
    return _summary_payload(store.sidebar_thread_summary(item), state)


def _scope_items(state: WebAppState, items: list[Any], project_id: str | None) -> list[Any]:
    return [item for item in items if _project_id(state, item.thread.session_id) == project_id]


def _complete_scope_order(state: WebAppState, items: list[Any], project_id: str | None) -> list[str]:
    known = {item.thread.thread_id for item in items}
    stored = [item for item in state.projects.sidebar_thread_order(project_id) if item in known]
    stored_ids = set(stored)
    missing = sorted(
        (item for item in items if item.thread.thread_id not in stored_ids),
        key=lambda item: (item.thread.created_at, item.thread.thread_id),
        reverse=True,
    )
    return [item.thread.thread_id for item in missing] + stored


def _active_scope_order(state: WebAppState, store, project_id: str | None) -> tuple[list[Any], list[str], list[str]]:
    scope_items = _scope_items(state, store.list_sidebar_thread_summaries(state="all"), project_id)
    active = [item for item in scope_items if item.thread.state == "active"]
    full_order = _complete_scope_order(state, scope_items, project_id)
    active_ids = {item.thread.thread_id for item in active}
    return active, full_order, [thread_id for thread_id in full_order if thread_id in active_ids]


def _save_active_scope_order(
    state: WebAppState,
    project_id: str | None,
    full_order: list[str],
    active_order: list[str],
) -> None:
    active_ids = set(active_order)
    replacements = iter(active_order)
    merged = [next(replacements) if thread_id in active_ids else thread_id for thread_id in full_order]
    state.projects.save_sidebar_thread_order(project_id, merged)


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
    web: WebAppState = request.app.state.web
    store = session_store(web)
    items = store.list_sidebar_thread_summaries(state=state)
    if state in {"active", "all"}:
        all_items = store.list_sidebar_thread_summaries(state="all")
        groups: dict[str | None, list[Any]] = {}
        for item in items:
            groups.setdefault(_project_id(web, item.thread.session_id), []).append(item)
        ordered: list[Any] = []
        for project_id, group in groups.items():
            by_id = {item.thread.thread_id: item for item in group}
            scope_items = _scope_items(web, all_items, project_id)
            full_order = _complete_scope_order(web, scope_items, project_id)
            ordered.extend(by_id[thread_id] for thread_id in full_order if thread_id in by_id)
        items = ordered
    return [_summary_payload(item, web) for item in items]


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
    return _payload(store, item, state)


@router.put("/order")
def update_sidebar_thread_order(
    body: SidebarThreadOrderRequest,
    request: Request,
) -> dict[str, list[str]]:
    if (body.ordered_thread_ids is None) == (body.sort_by is None):
        raise HTTPException(status_code=422, detail="ordered_thread_ids 与 sort_by 必须且只能提供一个。")
    web: WebAppState = request.app.state.web
    store = session_store(web)
    active, full_order, current_order = _active_scope_order(web, store, body.project_id)
    current_ids = {item.thread.thread_id for item in active}
    if body.ordered_thread_ids is not None:
        requested = body.ordered_thread_ids
        if len(requested) != len(set(requested)) or set(requested) != current_ids:
            raise HTTPException(status_code=409, detail="对话顺序必须完整且只能包含当前分组中的活动对话。")
        next_order = requested
    else:
        by_id = {item.thread.thread_id: item for item in active}
        if body.sort_by == "created_at":
            next_order = sorted(
                current_order,
                key=lambda thread_id: (by_id[thread_id].thread.created_at, thread_id),
                reverse=True,
            )
        else:
            next_order = sorted(
                current_order,
                key=lambda thread_id: (
                    by_id[thread_id].thread.last_activity_at,
                    by_id[thread_id].thread.created_at,
                    thread_id,
                ),
                reverse=True,
            )
    _save_active_scope_order(web, body.project_id, full_order, next_order)
    return {"ordered_thread_ids": next_order}


@router.patch("/{thread_id}")
def rename_sidebar_thread(
    thread_id: str,
    body: RenameSidebarThreadRequest,
    request: Request,
) -> dict[str, object]:
    web: WebAppState = request.app.state.web
    store = session_store(web)
    _require(store, thread_id)
    return _payload(
        store,
        store.update_sidebar_thread(thread_id, title=body.title.strip(), title_is_custom=True),
        web,
    )


@router.post("/{thread_id}/archive")
def archive_sidebar_thread(thread_id: str, request: Request) -> dict[str, object]:
    web: WebAppState = request.app.state.web
    store = session_store(web)
    _require(store, thread_id)
    from backend.domain.state import utc_now

    return _payload(store, store.update_sidebar_thread(thread_id, archived_at=utc_now(), deleted_at=None), web)


@router.post("/{thread_id}/restore")
def restore_sidebar_thread(thread_id: str, request: Request) -> dict[str, object]:
    web: WebAppState = request.app.state.web
    store = session_store(web)
    _require(store, thread_id)
    return _payload(store, store.update_sidebar_thread(thread_id, archived_at=None, deleted_at=None), web)


@router.delete("/{thread_id}")
def delete_sidebar_thread(thread_id: str, request: Request) -> dict[str, object]:
    web: WebAppState = request.app.state.web
    store = session_store(web)
    _require(store, thread_id)
    from backend.domain.state import utc_now

    return _payload(store, store.update_sidebar_thread(thread_id, deleted_at=utc_now()), web)


__all__ = ["router"]
