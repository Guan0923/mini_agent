"""Turn-centered tree operations and execution SSE endpoints."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator

from backend.domain import PlanningError
from backend.domain.runtime_state import (
    NodeFrame,
    RuntimeState,
    RuntimeStateValidationError,
    new_node_id,
    new_thread_id,
)
from backend.sandbox import SandboxInitializationError

from .active_turn_stream import ActiveTurnStream
from .auth.dependencies import require_user
from .auth.types import UserIdentity
from .chat.routes import (
    RuntimeModelRequest,
    _model_config_snapshot,
    _runtime_stream_lock_registry,
    _startup_failure_message,
    _stream,
)
from .session_store import require_active_session, session_store
from .shared.runtime import build_user_application
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


class RuntimeModelPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reasoning_effort: Literal["low", "medium", "high", "xhigh", "max"] | None = None
    current_model: str | None = Field(default=None, min_length=1, max_length=500)
    context_length: int | None = Field(default=None, gt=1)
    output_length: int | None = Field(default=None, ge=1)
    thinking: Literal["enable", "disable"] | None = None
    temperature: float | None = Field(default=None, ge=0, le=2)


class SteerTurnRequest(BaseModel):
    steering_id: str = Field(min_length=1, max_length=200)
    message: dict[str, object]


class TurnConfigPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider_name: str | None = Field(default=None, min_length=1, max_length=80)
    model: RuntimeModelPatch | None = None
    permission_mode: PermissionMode | None = None
    running_mode: RunningMode | None = None
    full_access_acknowledged: StrictBool | None = None

    @model_validator(mode="after")
    def validate_full_access(self):
        if self.permission_mode == "full_access" and self.full_access_acknowledged is not True:
            raise ValueError("full_access requires explicit joint file and network confirmation")
        return self


class ForkTurnRequest(BaseModel):
    id: str | None = Field(default=None, min_length=1, max_length=200)
    thread_id: str | None = Field(default=None, min_length=1, max_length=200)


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
    if item.get("type") != "text" or not isinstance(item.get("text"), str):
        raise HTTPException(status_code=422, detail="当前交互要求一个 text Item。")
    if not str(item["text"]).strip() and not item.get("references"):
        raise HTTPException(status_code=422, detail="text 或文件引用至少需要一个。")
    return item


def _references(item: Mapping[str, object]) -> list[dict[str, str]]:
    raw = item.get("references", [])
    if not isinstance(raw, list):
        raise HTTPException(status_code=422, detail="references 必须为 list。")
    result: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for value in raw:
        if not isinstance(value, Mapping) or value.get("source") not in {"project", "upload"}:
            raise HTTPException(status_code=422, detail="无效的文件引用。")
        path = value.get("path")
        if not isinstance(path, str) or not path:
            raise HTTPException(status_code=422, detail="文件引用 path 不能为空。")
        key = (str(value["source"]), path)
        if key not in seen:
            seen.add(key)
            result.append({"source": key[0], "path": key[1]})
    return result


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


@router.get("/{turn_id}/stream")
def stream_running_turn(
    turn_id: str, request: Request, identity: UserIdentity = Depends(require_user)
) -> StreamingResponse:
    state: WebAppState = request.app.state.web
    store = session_store(state, identity.id)
    turn = _turn(store, turn_id)
    active_streams = getattr(state, "active_turn_streams", {})
    lock = getattr(state, "active_turn_streams_lock", None)
    if hasattr(lock, "__enter__"):
        with lock:
            active_stream = active_streams.get((identity.id, turn_id))
    else:
        active_stream = active_streams.get((identity.id, turn_id))
    if isinstance(active_stream, ActiveTurnStream):
        subscription = active_stream.subscribe(turn_id)
        return StreamingResponse(subscription.as_sse(), media_type="text/event-stream")

    async def completed_stream():
        yield NodeFrame.snapshot(turn).as_sse()
        terminal_type = "success" if turn.status in {"success", "paused"} else "failed"
        yield f'data: <SSE id="{turn.id}" type="{terminal_type}"></SSE>\n\n'

    if turn.status != "running":
        return StreamingResponse(completed_stream(), media_type="text/event-stream")
    raise HTTPException(status_code=409, detail="运行流正在切换，请重新加载 Turn。")


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

    def operation(
        conversation,
        interrupt,
        sink,
        cancel_requested,
        suspend_requested,
        request_parameters,
        steering,
    ):
        return conversation.resume_session(
            source.session_id,
            on_event=sink,
            interrupt=interrupt,
            cancel_requested=cancel_requested,
            suspend_requested=suspend_requested,
            request_parameters=request_parameters,
            steering=steering,
            resume_confirmed=True,
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


@router.post("/{turn_id}/steer", status_code=202)
def steer_turn(
    turn_id: str,
    body: SteerTurnRequest,
    request: Request,
    identity: UserIdentity = Depends(require_user),
) -> dict[str, str]:
    state: WebAppState = request.app.state.web
    store = session_store(state, identity.id)
    source = _turn(store, turn_id)
    if source.status != "running":
        raise HTTPException(status_code=409, detail="只有 running Turn 可以接收新输入。")
    item = _user_item(body.message)
    normalized_message = {
        "role": "user",
        "content": [
            {
                "type": "text",
                "text": str(item["text"]).strip(),
                **({"references": _references(item)} if item.get("references") else {}),
            }
        ],
    }
    inbox = getattr(state, "active_turn_steering", {}).get((identity.id, turn_id))
    if inbox is None or not inbox.put(body.steering_id, normalized_message):
        raise HTTPException(status_code=409, detail="Turn 执行流已经封闭。")
    return {"steering_id": body.steering_id, "status": "accepted"}


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
    try:
        forked = store.fork_turn_node(turn_id, new_turn_id=body.id, thread_id=thread_id)
        sidebar = store.create_sidebar_thread(
            session_id=source.session_id,
            thread_id=thread_id,
            title=f"{source_sidebar.title}（分支）",
            title_is_custom=False,
        )
    except (ValueError, RuntimeStateValidationError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"turn": forked.to_dict(), "sidebar_thread": sidebar.to_dict()}


@router.post("/{turn_id}/compact", status_code=201)
def compact_turn(
    turn_id: str,
    request: Request,
    identity: UserIdentity = Depends(require_user),
) -> dict[str, object]:
    state: WebAppState = request.app.state.web
    store = session_store(state, identity.id)
    source = _turn(store, turn_id)
    if source.status != "success":
        raise HTTPException(status_code=409, detail="只有 success Turn 可以压缩。")

    stream_locks = _runtime_stream_lock_registry(state)
    stream_lock = stream_locks["__lock__"]
    stream_keys = stream_locks["keys"]
    stream_key = (identity.id, source.thread_id)
    with stream_lock:
        if stream_key in stream_keys:
            raise HTTPException(status_code=409, detail="当前 Thread 已有 running Turn。")
        stream_keys.add(stream_key)

    app = None
    try:
        workspace = state.session_workspace(identity.id, source.session_id)
        bound_project = state.projects(identity.id).session_project(source.session_id, include_removed=False)
        if bound_project is not None and not bound_project.available:
            raise HTTPException(status_code=409, detail="项目 cwd 不可访问，请恢复文件夹后重试。")
        model_config = _model_config_snapshot(
            state,
            identity.id,
            provider_name=source.provider_name,
        )
        app = build_user_application(
            state,
            identity.id,
            session_id=source.session_id,
            user_preferences=state.agent_preferences_for_user(identity.id),
            model_config=model_config,
            load_model_config=False,
            workspace=workspace,
            project_id=bound_project.project_id if bound_project is not None else None,
            job_registry=state.job_registry,
        )
        conversation = app.open_conversation(source.session_id)
        compacted = conversation.compact_turn(source.id, new_node_id())
        return compacted.to_dict()
    except HTTPException:
        raise
    except SandboxInitializationError as exc:
        raise HTTPException(status_code=503, detail=_startup_failure_message(exc)) from exc
    except PlanningError as exc:
        raise HTTPException(status_code=502, detail="上下文压缩失败，请稍后重试。") from exc
    except (KeyError, ValueError, RuntimeStateValidationError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail="上下文压缩失败，请稍后重试。") from exc
    finally:
        if app is not None:
            app.close()
        with stream_lock:
            stream_keys.discard(stream_key)


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
    if node.status != "running":
        raise HTTPException(status_code=409, detail="只有 running Turn 可以修改运行配置。")
    changes: dict[str, object] = {}
    if body.provider_name is not None:
        changes["provider_name"] = body.provider_name
    if body.model is not None:
        merged_model = {**node.model, **body.model.model_dump(exclude_none=True)}
        try:
            changes["model"] = RuntimeModelRequest.model_validate(merged_model).model_dump()
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
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
