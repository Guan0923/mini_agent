"""Persistent right-panel windows and interactive terminal transport."""

from __future__ import annotations

import asyncio
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, ConfigDict, Field

from backend.domain import MessageQueueUnavailable, RightPanelWindow
from backend.domain.runtime_state import RuntimeRootState, RuntimeState, new_thread_id
from backend.domain.state import utc_now
from backend.domain.terminal import TERMINAL_LABELS
from backend.tools.terminal import available_terminal_executables

from ..security import LocalWebSettings, browser_origin_allowed
from ..session_store import require_active_session, session_store
from ..state import WebAppState

router = APIRouter(prefix="/api/right-panel", tags=["right-panel"])


class RightPanelStatePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    width: int | None = Field(default=None, ge=0, le=100_000)
    collapsed: bool | None = None
    active_window_id: str | None = Field(default=None, max_length=200)


class CreatePanelWindowRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_turn_id: str = Field(min_length=1, max_length=200)


class RenamePanelWindowRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=120)


def _turn(store, session_id: str, turn_id: str) -> RuntimeState:
    item = store.find_node(turn_id)
    if item is None or isinstance(item, RuntimeRootState) or item.session_id != session_id:
        raise HTTPException(status_code=404, detail="未知 Turn。")
    if item.thread_id != item.session_id:
        raise HTTPException(status_code=409, detail="侧聊只能从主聊天当前 Turn 创建。")
    return item


def _window(store, session_id: str, window_id: str) -> RightPanelWindow:
    item = store.get_right_panel_window(session_id, window_id)
    if item is None or not item.active:
        raise HTTPException(status_code=404, detail="未知右栏窗口。")
    return item


def _payload(state: WebAppState, session_id: str) -> dict[str, object]:
    store = session_store(state)
    windows = store.list_right_panel_windows(session_id)
    stale = [
        item
        for item in windows
        if item.kind == "terminal"
        and item.terminal_id is not None
        and state.terminal_manager.get(item.terminal_id) is None
    ]
    for item in stale:
        store.update_right_panel_window(session_id, item.id, deleted_at=utc_now())
    if stale:
        windows = store.list_right_panel_windows(session_id)
    panel = store.get_right_panel_state(session_id)
    active_ids = {item.id for item in windows}
    if panel.active_window_id not in active_ids:
        active_window_id = windows[0].id if windows else None
        panel = store.save_right_panel_state(session_id, active_window_id=active_window_id)
    terminal_type = state.settings.runtime_config().get("terminal_type", "cmd")
    terminal_available = terminal_type in available_terminal_executables()
    terminal_reason = (
        None
        if terminal_available
        else f"配置的终端 {TERMINAL_LABELS.get(str(terminal_type), terminal_type)} 当前不可用。"
    )
    return {
        "state": panel.to_dict(),
        "windows": [item.to_dict() for item in windows],
        "capabilities": {
            "terminal_available": terminal_available,
            "terminal_unavailable_reason": terminal_reason,
        },
    }


@router.get("/{session_id}")
def get_right_panel(session_id: str, request: Request) -> dict[str, object]:
    store = session_store(request.app.state.web)
    require_active_session(store, session_id)
    return _payload(request.app.state.web, session_id)


@router.patch("/{session_id}")
def update_right_panel(
    session_id: str,
    body: RightPanelStatePatch,
    request: Request,
) -> dict[str, object]:
    store = session_store(request.app.state.web)
    require_active_session(store, session_id)
    kwargs: dict[str, object] = {}
    if "width" in body.model_fields_set:
        kwargs["width"] = body.width
    if "collapsed" in body.model_fields_set:
        kwargs["collapsed"] = body.collapsed
    if "active_window_id" in body.model_fields_set:
        if body.active_window_id is not None:
            _window(store, session_id, body.active_window_id)
        kwargs["active_window_id"] = body.active_window_id
    store.save_right_panel_state(session_id, **kwargs)
    return _payload(request.app.state.web, session_id)


@router.post("/{session_id}/side-chats", status_code=201)
def create_side_chat(
    session_id: str,
    body: CreatePanelWindowRequest,
    request: Request,
) -> dict[str, object]:
    state: WebAppState = request.app.state.web
    store = session_store(state)
    require_active_session(store, session_id)
    source = _turn(store, session_id, body.source_turn_id)
    thread_id = new_thread_id()
    anchor = store.build_side_chat_anchor(source.id, thread_id=thread_id)
    all_windows = store.list_right_panel_windows(session_id, include_deleted=True)
    number = sum(item.kind == "side_chat" for item in all_windows) + 1
    now = utc_now()
    window = RightPanelWindow(
        id=f"window_{uuid4().hex}",
        session_id=session_id,
        kind="side_chat",
        title=f"侧聊 {number}",
        position=len(all_windows),
        created_at=now,
        updated_at=now,
        thread_id=thread_id,
        anchor_turn_id=anchor.id,
    )
    try:
        store.create_side_chat_window(window, anchor)
        store.save_right_panel_state(session_id, collapsed=False, active_window_id=window.id)
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"window": window.to_dict(), "anchor": anchor.to_dict()}


@router.post("/{session_id}/terminals", status_code=201)
def create_terminal(
    session_id: str,
    body: CreatePanelWindowRequest,
    request: Request,
) -> dict[str, object]:
    state: WebAppState = request.app.state.web
    store = session_store(state)
    require_active_session(store, session_id)
    source = _turn(store, session_id, body.source_turn_id)
    if not source.cwd:
        raise HTTPException(status_code=409, detail="当前 Turn 没有可用 cwd。")
    terminal_type = state.settings.runtime_config().get("terminal_type", "cmd")
    try:
        terminal = state.terminal_manager.create(terminal_type, source.cwd)
    except MessageQueueUnavailable as exc:
        raise HTTPException(status_code=503, detail="message_queue_unavailable") from exc
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    all_windows = store.list_right_panel_windows(session_id, include_deleted=True)
    number = sum(item.kind == "terminal" and item.terminal_type == terminal.terminal_type for item in all_windows) + 1
    now = utc_now()
    window = RightPanelWindow(
        id=f"window_{uuid4().hex}",
        session_id=session_id,
        kind="terminal",
        title=f"{TERMINAL_LABELS[terminal.terminal_type]} {number}",
        position=len(all_windows),
        created_at=now,
        updated_at=now,
        terminal_id=terminal.id,
        terminal_type=terminal.terminal_type,
        cwd=terminal.cwd,
    )
    try:
        store.create_right_panel_window(window)
        store.save_right_panel_state(session_id, collapsed=False, active_window_id=window.id)
    except (RuntimeError, ValueError) as exc:
        state.terminal_manager.close(terminal.id)
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"window": window.to_dict(), "terminal": terminal.payload()}


@router.patch("/{session_id}/windows/{window_id}")
def rename_window(
    session_id: str,
    window_id: str,
    body: RenamePanelWindowRequest,
    request: Request,
) -> dict[str, object]:
    store = session_store(request.app.state.web)
    require_active_session(store, session_id)
    _window(store, session_id, window_id)
    try:
        return store.update_right_panel_window(session_id, window_id, title=body.title).to_dict()
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.delete("/{session_id}/windows/{window_id}", status_code=204)
def close_window(session_id: str, window_id: str, request: Request) -> None:
    state: WebAppState = request.app.state.web
    store = session_store(state)
    require_active_session(store, session_id)
    window = _window(store, session_id, window_id)
    store.update_right_panel_window(session_id, window_id, deleted_at=utc_now())
    if window.kind == "terminal" and window.terminal_id is not None:
        state.terminal_manager.close(window.terminal_id)
    elif window.thread_id is not None:
        runtime_thread = store.get_runtime_thread(session_id, window.thread_id)
        running_turn_id = runtime_thread.running_turn_id if runtime_thread is not None else None
        if running_turn_id:
            controller = getattr(state, "active_turn_cancellations", {}).get(running_turn_id)
            request_pause = getattr(controller, "request_pause", None)
            try:
                if callable(request_pause):
                    request_pause()
                else:
                    store.pause_turn(running_turn_id)
            except (KeyError, RuntimeError, ValueError):
                pass
    remaining = store.list_right_panel_windows(session_id)
    current = store.get_right_panel_state(session_id)
    if current.active_window_id == window_id:
        store.save_right_panel_state(session_id, active_window_id=remaining[0].id if remaining else None)


async def _terminal_output(websocket: WebSocket, state: WebAppState, terminal_id: str, after: int) -> None:
    sequence = after
    while True:
        chunks = await asyncio.to_thread(state.terminal_manager.wait_after, terminal_id, sequence)
        for chunk in chunks:
            await websocket.send_json({"type": "output", "sequence": chunk.sequence, "data": chunk.data})
            sequence = chunk.sequence
        terminal = state.terminal_manager.get(terminal_id)
        if terminal is None:
            return
        if terminal.exit_code is not None:
            await websocket.send_json({"type": "exit", "code": terminal.exit_code, "last_sequence": sequence})
            return


async def _terminal_input(websocket: WebSocket, state: WebAppState, terminal_id: str) -> None:
    while True:
        payload = await websocket.receive_json()
        if not isinstance(payload, dict):
            await websocket.close(code=1003)
            return
        kind = payload.get("type")
        try:
            if kind == "input" and isinstance(payload.get("data"), str):
                state.terminal_manager.write(terminal_id, payload["data"])
            elif kind == "resize" and isinstance(payload.get("cols"), int) and isinstance(payload.get("rows"), int):
                state.terminal_manager.resize(terminal_id, payload["cols"], payload["rows"])
            else:
                raise ValueError("Invalid terminal WebSocket message.")
        except (KeyError, ValueError):
            await websocket.close(code=1003)
            return


@router.websocket("/terminals/{terminal_id}/ws")
async def terminal_websocket(terminal_id: str, websocket: WebSocket) -> None:
    settings = LocalWebSettings.from_env()
    if not browser_origin_allowed(websocket.headers.get("origin"), settings):
        await websocket.close(code=1008)
        return
    state: WebAppState = websocket.app.state.web
    try:
        terminal = state.terminal_manager.connect(terminal_id)
    except KeyError:
        await websocket.close(code=1008)
        return
    try:
        try:
            after = max(0, int(websocket.query_params.get("after_sequence", "0")))
        except ValueError:
            await websocket.close(code=1003)
            return
        await websocket.accept()
        await websocket.send_json({"type": "ready", **terminal.payload()})
        output = asyncio.create_task(_terminal_output(websocket, state, terminal_id, after))
        input_task = asyncio.create_task(_terminal_input(websocket, state, terminal_id))
        done, pending = await asyncio.wait({output, input_task}, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        for task in done:
            task.result()
    except WebSocketDisconnect:
        pass
    finally:
        state.terminal_manager.disconnect(terminal_id)


__all__ = ["router"]
