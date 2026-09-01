"""Turn-centered tree operations and execution SSE endpoints."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from backend.domain import MessageEnvelope, PlanningError
from backend.domain.runtime_state import (
    RuntimeState,
    RuntimeStateValidationError,
    new_node_id,
    new_thread_id,
)
from backend.sandbox import SandboxInitializationError

from ..agent_report_projection import project_turn
from ..chat.routes import (
    RuntimeModelRequest,
    _model_config_snapshot,
    _runtime_stream_lock_registry,
    _startup_failure_message,
)
from ..runtime_event_transport import turn_sse
from ..session_store import require_active_session, session_store
from ..shared.runtime import build_local_application
from ..state import WebAppState
from .turn_models import (
    CreateTurnRequest,
    CurrentDataRequest,
    ForkTurnRequest,
    RewindTurnRequest,
    SteerTurnRequest,
    TurnConfigPatch,
    TurnExecutionConfig,
)
from .turn_support import _queue_http_error, _references, _stream_turn, _turn, _user_item

router = APIRouter(prefix="/api/turns", tags=["turns"])


@router.get("")
def list_turns(session_id: str, request: Request) -> list[dict[str, object]]:
    store = session_store(request.app.state.web)
    require_active_session(store, session_id)
    return [
        project_turn(store, item) if isinstance(item, RuntimeState) else item.to_dict()
        for item in store.load_nodes(session_id)
        if item.session_id == session_id
    ]


@router.get("/{turn_id}/trace")
def get_turn_trace(
    turn_id: str,
    data_idx: int,
    request: Request,
    after_sequence: int | None = Query(default=None, ge=0),
) -> dict[str, object]:
    store = session_store(request.app.state.web)
    turn = _turn(store, turn_id)
    if data_idx < 0 or data_idx >= len(turn.data):
        raise HTTPException(status_code=422, detail="data_idx 超出 Turn 版本范围。")
    try:
        trace = store.load_turn_trace(
            turn.session_id,
            turn.id,
            data_idx,
            after_sequence=after_sequence,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {
        "turn": project_turn(store, turn),
        "data_idx": data_idx,
        "context": trace.context.to_dict() if trace is not None and after_sequence is None else None,
        "items": [item.to_dict() for item in trace.items] if trace is not None else [],
        "last_sequence": trace.last_sequence if trace is not None else 0,
    }


@router.get("/{turn_id}/stream")
def stream_running_turn(
    turn_id: str,
    request: Request,
    session_id: str | None = None,
    thread_id: str | None = None,
    delivery_id: str | None = None,
) -> StreamingResponse:
    state: WebAppState = request.app.state.web
    store = session_store(state)
    found = store.find_node(turn_id)
    if found is None:
        if not session_id:
            raise HTTPException(status_code=404, detail="未知 Turn。")
        require_active_session(store, session_id)
        resolved_session_id = session_id
        resolved_thread_id = thread_id or session_id
    elif isinstance(found, RuntimeState):
        resolved_session_id = found.session_id
        resolved_thread_id = found.thread_id
    else:
        raise HTTPException(status_code=409, detail="根 Turn 仅作为消息树锚点，不能执行 Turn 操作。")
    return StreamingResponse(
        turn_sse(
            state,
            resolved_session_id,
            resolved_thread_id,
            turn_id,
            request.headers.get("last-event-id"),
            delivery_id,
        ),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@router.post("", status_code=202)
async def create_turn(body: CreateTurnRequest, request: Request) -> dict[str, object]:
    state: WebAppState = request.app.state.web
    store = session_store(state)
    require_active_session(store, body.session_id)
    sidebar = store.get_sidebar_thread(body.thread_id)
    panel_window = store.active_right_panel_window_for_thread(body.session_id, body.thread_id)
    if (sidebar is None or sidebar.session_id != body.session_id or sidebar.state != "active") and panel_window is None:
        raise HTTPException(status_code=409, detail="Thread 不可用。")
    delivery_id = (
        body.queued_delivery.delivery_id
        if body.queued_delivery is not None
        else body.delivery_id or f"turn-start:{body.id}"
    )
    existing = store.find_node(body.id)
    if isinstance(existing, RuntimeState):
        if any(message.get("delivery_id") == delivery_id for version in existing.data for message in version):
            return {"turn_id": body.id, "delivery_id": delivery_id, "status": "accepted"}
        raise HTTPException(status_code=409, detail="Turn id 已存在。")
    parent = _turn(store, body.parent_id) if body.parent_id else None
    if parent is not None:
        if parent.session_id != body.session_id or parent.thread_id != body.thread_id:
            raise HTTPException(status_code=409, detail="parent Turn 不属于当前 Thread。")
        if parent.status == "running":
            raise HTTPException(status_code=409, detail="不能在 running Turn 后创建孩子。")
    elif any(
        isinstance(node, RuntimeState) and node.thread_id == body.thread_id
        for node in store.load_nodes(body.session_id)
    ):
        raise HTTPException(status_code=409, detail="非首个 Turn 必须提供 parent_id。")
    try:
        state.message_queue.ping()
    except Exception as exc:
        raise _queue_http_error(exc) from exc
    command_payload = {
        "version": 1,
        "operation": "create",
        "parent_id": body.parent_id,
        "config": body.model_dump(
            include={
                "provider_name",
                "model",
                "permission_mode",
                "running_mode",
                "full_access_acknowledged",
            }
        ),
    }
    if body.queued_delivery is not None:
        try:
            state.message_queue.dispatch(
                delivery_id=body.queued_delivery.delivery_id,
                message_ids=body.queued_delivery.message_ids,
                session_id=body.session_id,
                thread_id=body.thread_id,
                turn_id=body.id,
                command_payload=command_payload,
            )
        except Exception as exc:
            raise _queue_http_error(exc) from exc
    else:
        assert body.message is not None
        item = _user_item(body.message)
        envelope = MessageEnvelope(
            delivery_id=delivery_id,
            sender_kind="user",
            source_thread_id=body.thread_id,
            target_kind="turn_start",
            target_id=body.id,
            session_id=body.session_id,
            thread_id=body.thread_id,
            payload={
                "content": str(item["text"]).strip(),
                "references": _references(item),
                **command_payload,
            },
            source_message_ids=(delivery_id,),
        )
        try:
            state.message_queue.dispatch_turn_start(envelope)
        except Exception as exc:
            raise _queue_http_error(exc) from exc
    return {"turn_id": body.id, "delivery_id": delivery_id, "status": "accepted"}


@router.post("/{turn_id}/rewind", status_code=202)
async def rewind_turn(turn_id: str, body: RewindTurnRequest, request: Request) -> dict[str, object]:
    state: WebAppState = request.app.state.web
    store = session_store(state)
    source = _turn(store, turn_id)
    item = _user_item(body.message)
    try:
        state.message_queue.ping()
    except Exception as exc:
        raise _queue_http_error(exc) from exc
    delivery_id = body.delivery_id or f"turn-rewind:{turn_id}:{len(source.data)}"
    envelope = MessageEnvelope(
        delivery_id=delivery_id,
        sender_kind="user",
        source_thread_id=source.thread_id,
        target_kind="turn_start",
        target_id=source.id,
        session_id=source.session_id,
        thread_id=source.thread_id,
        payload={
            "content": str(item["text"]).strip(),
            "references": _references(item),
            "version": 1,
            "operation": "rewind",
            "config": body.model_dump(
                include={
                    "provider_name",
                    "model",
                    "permission_mode",
                    "running_mode",
                    "full_access_acknowledged",
                }
            ),
        },
        source_message_ids=(delivery_id,),
    )
    try:
        state.message_queue.dispatch_turn_start(envelope)
    except Exception as exc:
        raise _queue_http_error(exc) from exc
    return {"turn_id": source.id, "delivery_id": delivery_id, "status": "accepted"}


@router.post("/{turn_id}/resume", status_code=202)
async def resume_turn(turn_id: str, body: TurnExecutionConfig, request: Request) -> dict[str, object]:
    state: WebAppState = request.app.state.web
    store = session_store(state)
    source = _turn(store, turn_id)
    if source.status != "paused":
        raise HTTPException(status_code=409, detail="只有 paused Turn 可以恢复。")
    try:
        state.message_queue.ping()
    except Exception as exc:
        raise _queue_http_error(exc) from exc

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

    _stream_turn(
        state,
        session_id=source.session_id,
        thread_id=source.thread_id,
        turn_id=source.id,
        prompt="",
        source_id=source.id,
        config=body,
        adopt_existing=True,
        operation=operation,
        stream_response=False,
    )
    return {"turn_id": source.id, "status": "accepted"}


@router.post("/{turn_id}/pause")
def pause_turn(turn_id: str, request: Request) -> dict[str, object]:
    state: WebAppState = request.app.state.web
    store = session_store(state)
    source = _turn(store, turn_id)
    controller = getattr(state, "active_turn_cancellations", {}).get(turn_id)
    request_pause = getattr(controller, "request_pause", None)
    if callable(request_pause):
        request_pause()
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
) -> dict[str, str]:
    state: WebAppState = request.app.state.web
    store = session_store(state)
    source = _turn(store, turn_id)
    if source.status != "running":
        raise HTTPException(status_code=409, detail="只有 running Turn 可以接收新输入。")
    active_stream = getattr(state, "active_turn_streams", {}).get(turn_id)
    if active_stream is None:
        raise HTTPException(status_code=409, detail="Turn 执行流已经封闭。")
    try:
        state.message_queue.dispatch(
            delivery_id=body.delivery_id,
            message_ids=body.message_ids,
            session_id=source.session_id,
            thread_id=source.thread_id,
            turn_id=source.id,
        )
    except Exception as exc:
        raise _queue_http_error(exc) from exc
    return {"delivery_id": body.delivery_id, "status": "accepted"}


@router.post("/{turn_id}/fork", status_code=201)
def fork_turn(turn_id: str, body: ForkTurnRequest, request: Request) -> dict[str, object]:
    store = session_store(request.app.state.web)
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
) -> dict[str, object]:
    state: WebAppState = request.app.state.web
    store = session_store(state)
    source = _turn(store, turn_id)
    if source.status != "success":
        raise HTTPException(status_code=409, detail="只有 success Turn 可以压缩。")

    stream_locks = _runtime_stream_lock_registry(state)
    stream_lock = stream_locks["__lock__"]
    stream_keys = stream_locks["keys"]
    stream_key = source.thread_id
    with stream_lock:
        if stream_key in stream_keys:
            raise HTTPException(status_code=409, detail="当前 Thread 已有 running Turn。")
        stream_keys.add(stream_key)

    app = None
    try:
        state.paths.ensure_session(source.session_id)
        workspace = state.paths.session_workspace(source.session_id)
        bound_project = state.projects.session_project(source.session_id, include_removed=False)
        if bound_project is not None and not bound_project.available:
            raise HTTPException(status_code=409, detail="项目 cwd 不可访问，请恢复文件夹后重试。")
        project_cwd = Path(bound_project.cwd).resolve() if bound_project is not None else None
        model_config = _model_config_snapshot(state, provider_name=source.provider_name)
        app = build_local_application(
            state,
            session_id=source.session_id,
            user_preferences=state.agent_preferences(),
            model_config=model_config,
            load_model_config=False,
            workspace=workspace,
            project_id=bound_project.project_id if bound_project is not None else None,
            project_cwd=project_cwd,
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
def patch_current_data(turn_id: str, body: CurrentDataRequest, request: Request) -> dict[str, object]:
    store = session_store(request.app.state.web)
    _turn(store, turn_id)
    try:
        return store.set_turn_current_data(turn_id, body.current_data_idx).to_dict()
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="未知 Turn。") from exc
    except RuntimeStateValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.patch("/{turn_id}/config")
def patch_turn_config(turn_id: str, body: TurnConfigPatch, request: Request) -> dict[str, object]:
    state: WebAppState = request.app.state.web
    store = session_store(state)
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
    bridge = getattr(state, "active_runtime_bridges", {}).get(node.thread_id)
    try:
        updated = bridge.apply_runtime_config(changes) if bridge is not None else None
        if updated is None:
            updated = state.subagent_coordinator.apply_runtime_config(node.session_id, node.thread_id, changes)
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


__all__ = ["TurnExecutionConfig", "router"]
