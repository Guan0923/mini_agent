"""Turn-centered tree operations and execution SSE endpoints."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, StrictBool, model_validator

from backend.domain.runtime_state import RuntimeState, RuntimeStateTree, RuntimeStateValidationError, new_thread_id

from .auth.dependencies import require_user
from .auth.types import UserIdentity
from .chat.routes import RuntimeModelRequest, _model_config_snapshot, _stream
from .session_store import require_active_session, session_store
from .state import WebAppState

router = APIRouter(prefix="/api/turns", tags=["turns"])
PermissionMode = Literal["read_only", "workspace_write", "full_access"]
RunningMode = Literal["agent", "plan"]


class TurnExecutionConfig(BaseModel):
    provider_name: str | None = Field(default=None, min_length=1, max_length=80)
    model: RuntimeModelRequest | None = None
    permission_mode: PermissionMode = "read_only"
    running_mode: RunningMode = "agent"
    full_access_acknowledged: StrictBool = False

    @model_validator(mode="after")
    def validate_full_access(self):
        if self.permission_mode == "full_access" and not self.full_access_acknowledged:
            raise ValueError("full_access requires explicit joint file and network confirmation")
        return self


class CreateTurnRequest(TurnExecutionConfig):
    id: str = Field(min_length=1, max_length=200)
    session_id: str = Field(min_length=1, max_length=200)
    thread_id: str = Field(min_length=1, max_length=200)
    parent_id: str = Field(default="", max_length=200)
    message: dict[str, object]


class RewindTurnRequest(TurnExecutionConfig):
    message: dict[str, object]


class CurrentDataRequest(BaseModel):
    current_data_idx: int = Field(ge=0)


class TurnConfigPatch(TurnExecutionConfig):
    provider_name: str | None = Field(default=None, min_length=1, max_length=80)
    model: RuntimeModelRequest | None = None
    permission_mode: PermissionMode | None = None
    running_mode: RunningMode | None = None


class ForkTurnRequest(BaseModel):
    id: str | None = Field(default=None, min_length=1, max_length=200)
    thread_id: str | None = Field(default=None, min_length=1, max_length=200)
    title: str | None = Field(default=None, max_length=120)


class CompactTurnRequest(BaseModel):
    summary: str | None = Field(default=None, min_length=1, max_length=200_000)
    id: str | None = Field(default=None, min_length=1, max_length=200)


def _turn(store, turn_id: str) -> RuntimeState:
    item = store.find_node(turn_id)
    if item is None:
        raise HTTPException(status_code=404, detail="未知 Turn。")
    return item


def _user_item(message: Mapping[str, object]) -> dict[str, object]:
    if message.get("role") != "user":
        raise HTTPException(status_code=422, detail="message.role 必须为 user。")
    content = message.get("content")
    if not isinstance(content, list) or len(content) != 1 or not isinstance(content[0], Mapping):
        raise HTTPException(status_code=422, detail="user Message 必须恰好包含一个 Item。")
    item = dict(content[0])
    if item.get("type") != "text" or not isinstance(item.get("text"), str) or not str(item["text"]).strip():
        raise HTTPException(status_code=422, detail="当前交互要求一个非空 text Item。")
    return item


def _references(item: Mapping[str, object]) -> list[dict[str, str]]:
    raw = item.get("references", [])
    if not isinstance(raw, list):
        raise HTTPException(status_code=422, detail="references 必须为 list。")
    result: list[dict[str, str]] = []
    for value in raw:
        if not isinstance(value, Mapping) or value.get("source") not in {"project", "upload"}:
            raise HTTPException(status_code=422, detail="无效的文件引用。")
        path = value.get("path")
        if not isinstance(path, str) or not path:
            raise HTTPException(status_code=422, detail="文件引用 path 不能为空。")
        result.append({"source": str(value["source"]), "path": path})
    return result


def _compaction_summary(store, source: RuntimeState) -> str:
    path = RuntimeStateTree(store.load_nodes(source.session_id)).ancestors(source)
    start = next((index for index, turn in enumerate(path) if turn.id == source.compaction_id), None)
    if start is None:
        raise RuntimeStateValidationError("compactionId is not an ancestor of the Turn.")
    items = [item for turn in path[start:] for message in turn.selected_messages for item in message["content"]]
    prefix = items[: max(0, len(items) - source.first_kept_item_size)]
    lines: list[str] = []
    for item in prefix:
        kind = str(item.get("type") or "item")
        value = item.get("text", item.get("content", item.get("summary", "")))
        rendered = str(value).strip()
        if rendered:
            lines.append(f"[{kind}] {rendered[:2000]}")
    return "\n".join(lines)[-50_000:] or "此前上下文不含可摘要文本。"


def _stream_turn(
    state: WebAppState,
    identity: UserIdentity,
    *,
    session_id: str,
    thread_id: str,
    turn_id: str,
    prompt: str,
    source_id: str | None,
    config: TurnExecutionConfig,
    references: list[dict[str, str]] | None = None,
    adopt_existing: bool = False,
    operation=None,
) -> StreamingResponse:
    model = config.model
    stream = _stream(
        state,
        prompt,
        identity=identity,
        session_id=session_id,
        thread_id=thread_id,
        turn_id=turn_id,
        source_node_id=source_id,
        adopt_existing=adopt_existing,
        mode=config.running_mode or "agent",
        permission_mode=config.permission_mode or "read_only",
        reasoning_effort=model.reasoning_effort if model is not None else "medium",
        provider_name=config.provider_name,
        model_snapshot=model.model_dump() if model is not None else None,
        request_model=model,
        user_preferences=state.agent_preferences_for_user(identity.id),
        model_config=_model_config_snapshot(state, identity.id),
        references=references,
        operation=operation,
    )
    return StreamingResponse(stream, media_type="text/event-stream")


@router.get("")
def list_turns(
    session_id: str, request: Request, identity: UserIdentity = Depends(require_user)
) -> list[dict[str, object]]:
    store = session_store(request.app.state.web, identity.id)
    require_active_session(store, session_id)
    return [item.to_dict() for item in store.load_nodes(session_id) if item.session_id == session_id]


@router.post("")
async def create_turn(
    body: CreateTurnRequest, request: Request, identity: UserIdentity = Depends(require_user)
) -> StreamingResponse:
    state: WebAppState = request.app.state.web
    store = session_store(state, identity.id)
    require_active_session(store, body.session_id)
    sidebar = store.get_sidebar_thread(body.thread_id)
    if sidebar is None or sidebar.session_id != body.session_id or sidebar.state != "active":
        raise HTTPException(status_code=409, detail="SidebarThread 不可用。")
    if store.find_node(body.id) is not None:
        raise HTTPException(status_code=409, detail="Turn id 已存在。")
    parent = _turn(store, body.parent_id) if body.parent_id else None
    if parent is not None:
        if parent.session_id != body.session_id or parent.thread_id != body.thread_id:
            raise HTTPException(status_code=409, detail="parent Turn 不属于当前 Thread。")
        if parent.status == "running":
            raise HTTPException(status_code=409, detail="不能在 running Turn 后创建孩子。")
    elif any(node.thread_id == body.thread_id for node in store.load_nodes(body.session_id)):
        raise HTTPException(status_code=409, detail="非首个 Turn 必须提供 parent_id。")
    item = _user_item(body.message)
    return _stream_turn(
        state,
        identity,
        session_id=body.session_id,
        thread_id=body.thread_id,
        turn_id=body.id,
        prompt=str(item["text"]).strip(),
        source_id=body.parent_id or None,
        config=body,
        references=_references(item),
    )


@router.post("/{turn_id}/rewind")
async def rewind_turn(
    turn_id: str, body: RewindTurnRequest, request: Request, identity: UserIdentity = Depends(require_user)
) -> StreamingResponse:
    state: WebAppState = request.app.state.web
    store = session_store(state, identity.id)
    source = _turn(store, turn_id)
    item = _user_item(body.message)
    try:
        rewound = store.append_turn_version(turn_id, item)
    except (ValueError, RuntimeStateValidationError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _stream_turn(
        state,
        identity,
        session_id=source.session_id,
        thread_id=source.thread_id,
        turn_id=source.id,
        prompt=str(item["text"]).strip(),
        source_id=rewound.id,
        config=body,
        references=_references(item),
        adopt_existing=True,
    )


@router.post("/{turn_id}/resume")
async def resume_turn(
    turn_id: str, body: TurnExecutionConfig, request: Request, identity: UserIdentity = Depends(require_user)
) -> StreamingResponse:
    state: WebAppState = request.app.state.web
    store = session_store(state, identity.id)
    source = _turn(store, turn_id)
    if source.status != "paused":
        raise HTTPException(status_code=409, detail="只有 paused Turn 可以恢复。")

    def operation(conversation, interrupt, sink, cancel_requested, suspend_requested, request_parameters):
        return conversation.resume_session(
            source.session_id,
            on_event=sink,
            interrupt=interrupt,
            cancel_requested=cancel_requested,
            suspend_requested=suspend_requested,
            request_parameters=request_parameters,
        )

    return _stream_turn(
        state,
        identity,
        session_id=source.session_id,
        thread_id=source.thread_id,
        turn_id=source.id,
        prompt="",
        source_id=source.id,
        config=body,
        adopt_existing=True,
        operation=operation,
    )


@router.post("/{turn_id}/pause")
def pause_turn(turn_id: str, request: Request, identity: UserIdentity = Depends(require_user)) -> dict[str, object]:
    state: WebAppState = request.app.state.web
    store = session_store(state, identity.id)
    source = _turn(store, turn_id)
    cancel = getattr(state, "active_turn_cancellations", {}).get((identity.id, turn_id))
    if callable(cancel):
        cancel()
        return source.to_dict()
    try:
        return store.pause_turn(turn_id).to_dict()
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/{turn_id}/fork", status_code=201)
def fork_turn(
    turn_id: str, body: ForkTurnRequest, request: Request, identity: UserIdentity = Depends(require_user)
) -> dict[str, object]:
    store = session_store(request.app.state.web, identity.id)
    source = _turn(store, turn_id)
    source_sidebar = store.get_sidebar_thread(source.thread_id)
    if source_sidebar is None or source_sidebar.session_id != source.session_id:
        raise HTTPException(status_code=409, detail="源 SidebarThread 不可用。")
    thread_id = body.thread_id or new_thread_id()
    explicit_title = body.title.strip() if body.title else ""
    try:
        forked = store.fork_turn_node(turn_id, new_turn_id=body.id, thread_id=thread_id)
        sidebar = store.create_sidebar_thread(
            session_id=source.session_id,
            thread_id=thread_id,
            title=explicit_title or source_sidebar.title,
            title_is_custom=bool(explicit_title),
        )
    except (ValueError, RuntimeStateValidationError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"turn": forked.to_dict(), "sidebar_thread": sidebar.to_dict()}


@router.post("/{turn_id}/compact", status_code=201)
def compact_turn(
    turn_id: str, body: CompactTurnRequest, request: Request, identity: UserIdentity = Depends(require_user)
) -> dict[str, object]:
    store = session_store(request.app.state.web, identity.id)
    try:
        source = _turn(store, turn_id)
        compacted = store.create_compact_turn(
            turn_id,
            body.summary or _compaction_summary(store, source),
            new_turn_id=body.id,
        )
        compacted.status = "success"
        compacted = RuntimeState.from_dict(compacted.to_dict())
        store.finalize_node(compacted)
        return compacted.to_dict()
    except (KeyError, ValueError, RuntimeStateValidationError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.patch("/{turn_id}/current-data")
def patch_current_data(
    turn_id: str, body: CurrentDataRequest, request: Request, identity: UserIdentity = Depends(require_user)
) -> dict[str, object]:
    store = session_store(request.app.state.web, identity.id)
    try:
        return store.set_turn_current_data(turn_id, body.current_data_idx).to_dict()
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="未知 Turn。") from exc
    except RuntimeStateValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.patch("/{turn_id}/config")
def patch_turn_config(
    turn_id: str, body: TurnConfigPatch, request: Request, identity: UserIdentity = Depends(require_user)
) -> dict[str, object]:
    state: WebAppState = request.app.state.web
    store = session_store(state, identity.id)
    node = _turn(store, turn_id)
    changes: dict[str, object] = {}
    if body.provider_name is not None:
        changes["provider_name"] = body.provider_name
    if body.model is not None:
        changes["model"] = body.model.model_dump()
    if body.permission_mode is not None:
        changes["permission_mode"] = body.permission_mode
    if body.running_mode is not None:
        changes["running_mode"] = body.running_mode
    bridge = getattr(state, "active_runtime_bridges", {}).get((identity.id, node.thread_id))
    try:
        updated = bridge.apply_runtime_config(changes) if bridge is not None else None
        if updated is None:
            writer_node = node.clone()
            for key, value in changes.items():
                setattr(writer_node, key, value)
            writer_node = RuntimeState.from_dict(writer_node.to_dict())
            store.update_node(writer_node)
            updated = writer_node
    except (ValueError, RuntimeStateValidationError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return updated.to_dict()


__all__ = ["router"]
