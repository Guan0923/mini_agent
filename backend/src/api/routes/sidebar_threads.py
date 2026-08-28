"""CRUD API for user-visible SidebarThread metadata."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from ..session_store import session_store
from ..state import WebAppState

router = APIRouter(prefix="/api/sidebar-threads", tags=["sidebar-threads"])


class CreateSidebarThreadRequest(BaseModel):
    title: str = Field(default="新对话", max_length=120)
    client_id: str | None = Field(default=None, max_length=200)


class RenameSidebarThreadRequest(BaseModel):
    title: str = Field(min_length=1, max_length=120)


def _payload(item) -> dict[str, object]:
    return item.to_dict()


def _require(store, thread_id: str):
    item = store.get_sidebar_thread(thread_id)
    if item is None:
        raise HTTPException(status_code=404, detail="未知 SidebarThread。")
    return item


@router.get("")
def list_sidebar_threads(
    request: Request,
    state: str = "active",
) -> list[dict[str, object]]:
    if state not in {"active", "archived", "deleted", "all"}:
        raise HTTPException(status_code=422, detail="无效的 SidebarThread 状态。")
    store = session_store(request.app.state.web)
    return [_payload(item) for item in store.list_sidebar_threads(state=state)]


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
    return _payload(item)


@router.patch("/{thread_id}")
def rename_sidebar_thread(
    thread_id: str,
    body: RenameSidebarThreadRequest,
    request: Request,
) -> dict[str, object]:
    store = session_store(request.app.state.web)
    _require(store, thread_id)
    return _payload(store.update_sidebar_thread(thread_id, title=body.title.strip(), title_is_custom=True))


@router.post("/{thread_id}/archive")
def archive_sidebar_thread(thread_id: str, request: Request) -> dict[str, object]:
    store = session_store(request.app.state.web)
    _require(store, thread_id)
    from backend.domain.state import utc_now

    return _payload(store.update_sidebar_thread(thread_id, archived_at=utc_now(), deleted_at=None))


@router.post("/{thread_id}/restore")
def restore_sidebar_thread(thread_id: str, request: Request) -> dict[str, object]:
    store = session_store(request.app.state.web)
    _require(store, thread_id)
    return _payload(store.update_sidebar_thread(thread_id, archived_at=None, deleted_at=None))


@router.delete("/{thread_id}")
def delete_sidebar_thread(thread_id: str, request: Request) -> dict[str, object]:
    store = session_store(request.app.state.web)
    _require(store, thread_id)
    from backend.domain.state import utc_now

    return _payload(store.update_sidebar_thread(thread_id, deleted_at=utc_now()))


__all__ = ["router"]
