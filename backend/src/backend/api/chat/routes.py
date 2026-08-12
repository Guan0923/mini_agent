"""SSE chat endpoint that drives the real agent against a persistent workspace.

Supports two modes:
- default: auto-approve every tool call (used by the web frontend);
- ``interactive=True``: pauses on approvals and asks the client to decide via
  ``POST /api/decisions`` (used by the TUI client).
"""

from __future__ import annotations

import asyncio
import json
import queue
import threading
from collections.abc import Callable
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from backend.domain import DEFAULT_TIME_ZONE, FAILED_TERMINAL_MESSAGE, terminal_error_text
from backend.providers import ModelConfig, ModelConfigurationError
from backend.runtime import RunnerSettings, build_application
from backend.runtime.core.events import RuntimeEvent
from backend.runtime.node_bridge import RuntimeEventNodeBridge
from backend.storage.auth.crypto import SecretDecryptionError

from ..auth.dependencies import require_user
from ..auth.types import UserIdentity
from ..sessions.routes import _require_active, _store
from ..shared.runtime import build_user_application
from ..state import WebAppState
from .decisions import router as decisions_router
from .interrupts import auto_approve, make_interactive_interrupt

router = APIRouter(prefix="/api")
router.include_router(decisions_router)

ReasoningEffort = Literal["low", "medium", "high", "xhigh", "max"]
_HIDDEN_RECOVERABLE_EVENTS = frozenset(
    {"tool_failed", "tool_recovery", "model_repair", "model_retry", "replan_requested"}
)


class ChatRequest(BaseModel):
    prompt: str
    interactive: bool = False
    session_id: str | None = None
    mode: Literal["agent", "plan"] = "agent"
    permission_mode: Literal["approval_for_me", "full_access"] | None = None
    reasoning_effort: ReasoningEffort = "medium"
    source_node_id: str | None = None


class ResumeRequest(BaseModel):
    permission_mode: Literal["approval_for_me", "full_access"] = "approval_for_me"
    reasoning_effort: ReasoningEffort = "medium"
    source_node_id: str | None = None


def _reasoning_parameters(effort: ReasoningEffort) -> dict[str, object]:
    return {"thinking": {"type": "enabled"}, "reasoning_effort": effort}


def _truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return f"{value[:limit]}… ({len(value) - limit} chars omitted)"


def _event_payload(event: RuntimeEvent) -> dict:
    """A small, JSON-safe slice of an event's data, mirroring the TUI presenter."""
    data = event.data
    identifiers = {key: data[key] for key in ("session_id", "run_id") if data.get(key) is not None}
    if event.kind == "tool_call":
        return {
            **identifiers,
            "tool": data.get("tool") or data.get("name"),
            "call_id": data.get("call_id"),
            "arguments": data.get("arguments", {}),
        }
    if event.kind == "tool_result":
        return {
            **identifiers,
            "tool": data.get("tool"),
            "call_id": data.get("call_id"),
            "result": data.get("result", event.message),
        }
    if event.kind == "tool_failed":
        return {
            **identifiers,
            "tool": data.get("tool"),
            "call_id": data.get("call_id"),
            "result": data.get("error", data.get("result", event.message)),
        }
    if event.kind in {"plan", "plan_progress", "run_suspended", "run_resumed", "error"}:
        details = {key: data[key] for key in ("plan", "goal", "steps", "status", "reason") if key in data}
        return {**identifiers, **details}
    if event.kind == "run_finished":
        return {
            **identifiers,
            "status": event.message,
            "final_answer": _truncate(data.get("final_answer", ""), 6000),
            "duration_ms": data.get("duration_ms"),
            "model_calls": data.get("model_calls"),
            "tool_calls": data.get("tool_calls"),
            "active_skills": data.get("active_skills", []),
        }
    if event.kind in {"response_delta", "response_start", "thinking_delta"}:
        return {**identifiers, "content": event.message}
    return identifiers


def _model_config_snapshot(state: WebAppState, user_id: str) -> ModelConfig:
    try:
        return state.model_config_for_user(user_id)
    except SecretDecryptionError as exc:
        raise HTTPException(
            status_code=409,
            detail="当前提供商密钥无法解密，请在用户设置中重新填写 API Key。",
        ) from exc
    except ModelConfigurationError as exc:
        raise HTTPException(status_code=422, detail=f"模型未配置：{exc}") from exc


def _validate_source_node(store, session_id: str, source_node_id: str | None, *, resume: bool = False) -> None:
    """Validate the optimistic-concurrency source before opening an SSE stream."""

    if not source_node_id or not callable(getattr(store, "get_node", None)):
        return
    source = store.get_node(session_id, source_node_id)
    if source is None:
        raise HTTPException(status_code=400, detail="source_node_id 不属于当前会话")
    children = getattr(store, "list_children", lambda *_: [])(source.session_id, source.id)
    if children:
        raise HTTPException(status_code=409, detail="source_node_id 必须是当前会话的叶子节点")
    if resume and source.status not in {"failed", "abort"}:
        raise HTTPException(status_code=409, detail="只能从 failed 或 abort 节点恢复")


def _stream(
    state: WebAppState,
    prompt: str,
    interactive: bool = False,
    *,
    identity: UserIdentity | None = None,
    session_id: str | None = None,
    source_node_id: str | None = None,
    mode: Literal["agent", "plan"] = "agent",
    permission_mode: Literal["approval_for_me", "full_access"] | None = None,
    reasoning_effort: ReasoningEffort = "medium",
    user_preferences: str = "",
    default_timezone: str = DEFAULT_TIME_ZONE,
    model_config: ModelConfig | None = None,
    runtime_config: dict[str, object] | None = None,
    operation: Callable[..., object] | None = None,
):
    q: queue.Queue = queue.Queue()
    done = threading.Event()
    cancel_requested = threading.Event()
    finished: dict = {}
    bridge_ref: dict[str, RuntimeEventNodeBridge | None] = {"bridge": None}

    def sink(item) -> None:
        if cancel_requested.is_set():
            return
        bridge = bridge_ref["bridge"]
        if isinstance(item, dict):
            if bridge is not None:
                bridge.handle_input(item)
            # Decision requests remain an input-channel envelope: the node
            # update carries the durable approval/question block, while this
            # small request lets the client submit its response immediately.
            q.put(item)
            return
        if bridge is not None:
            bridge.handle(item)
            if getattr(item, "kind", "") in _HIDDEN_RECOVERABLE_EVENTS:
                return
            return
        if getattr(item, "kind", "") in _HIDDEN_RECOVERABLE_EVENTS:
            return
        payload = _event_payload(item)
        if item.kind == "run_finished":
            finished.update(payload)
        q.put({"type": "event", "kind": item.kind, "message": item.message, "data": payload})

    def enqueue_terminal(item: dict) -> None:
        if not cancel_requested.is_set():
            q.put(item)

    owner_id = identity.id if identity is not None else None
    if permission_mode == "full_access":
        interrupt = make_interactive_interrupt(
            sink,
            cancel_requested=cancel_requested.is_set,
            auto_approve_tools=True,
            owner_id=owner_id,
        )
    elif interactive or permission_mode == "approval_for_me":
        interrupt = make_interactive_interrupt(
            sink,
            cancel_requested=cancel_requested.is_set,
            owner_id=owner_id,
        )
    else:
        interrupt = auto_approve

    def worker() -> None:
        app = None
        try:
            workspace = (
                state.user_workspace(identity.id, session_id)
                if identity is not None and session_id is not None
                else state.chat_workspace
            )
            path_options = {"paths": state.user_paths(identity.id)} if identity is not None else {}
            if identity is not None:
                app = build_user_application(
                    state,
                    identity.id,
                    session_id=session_id,
                    user_preferences=user_preferences,
                    model_config=model_config,
                    load_model_config=False,
                )
            else:
                app = build_application(
                    workspace,
                    planner_name="llm",
                    settings=RunnerSettings(log_full_messages=True),
                    project_mcp_enabled=False,
                    user_preferences=user_preferences,
                    model_config=model_config,
                    config_override=runtime_config,
                    default_timezone=default_timezone,
                    **path_options,
                )
            conversation = app.open_conversation(session_id) if session_id else app.open_conversation()
            # The node id is the optimistic-concurrency boundary for the new
            # tree protocol.  Legacy conversations do not expose nodes yet,
            # so validation is delegated to the node store when available.
            node_store = getattr(app, "session_store", None) or getattr(app, "store", None)
            if source_node_id and node_store is not None and session_id:
                _validate_source_node(node_store, session_id, source_node_id)
            if callable(getattr(node_store, "create_node", None)):
                if getattr(conversation, "active_session", None) is None:
                    conversation.ensure_session(prompt or None)
                active_session = getattr(conversation, "active_session", None)
                if active_session is not None:
                    bridge_ref["bridge"] = RuntimeEventNodeBridge(
                        node_store,
                        session_id=active_session.session_id,
                        prompt=prompt,
                        source_node_id=source_node_id,
                        provider=getattr(model_config, "provider", "unknown") if model_config else "unknown",
                        model=getattr(model_config, "model", "unknown") if model_config else "unknown",
                        cwd=str(workspace),
                        emit=lambda frame: q.put(frame.to_dict()) if not cancel_requested.is_set() else None,
                    )
                    bridge_ref["bridge"].start()
            if operation is None:
                run_state = conversation.run_task(
                    prompt,
                    mode=mode,
                    on_event=sink,
                    interrupt=interrupt,
                    cancel_requested=cancel_requested.is_set,
                    request_parameters=_reasoning_parameters(reasoning_effort),
                )
            else:
                run_state = operation(
                    conversation,
                    interrupt,
                    sink,
                    cancel_requested.is_set,
                    _reasoning_parameters(reasoning_effort),
                )
            active_session = getattr(conversation, "active_session", None)
            runtime = getattr(conversation, "runtime", None)
            current_run = runtime.state.current_run if runtime is not None else None
            bridge = bridge_ref["bridge"]
            if bridge is not None:
                old_status = str(run_state.status if run_state is not None else "failed")
                stop_reason = str(getattr(run_state, "stop_reason", "") or "")
                if old_status in {"completed", "success"}:
                    requested_status = "success"
                    category = None
                elif old_status == "cancelled":
                    requested_status = "abort"
                    category = "user"
                elif bridge.abort_category is not None:
                    requested_status = "abort"
                    category = bridge.abort_category
                else:
                    requested_status = "failed"
                    category = None
                final_answer = (run_state.final_answer if run_state is not None else "") or ""
                final_node = bridge.finish(
                    requested_status,
                    final_answer,
                    category=category,
                    code=stop_reason or bridge.abort_code,
                )
                terminal_status = final_node.status if final_node is not None else requested_status
                terminal_error = bridge.terminal_error
                rendered_error = terminal_error_text(terminal_error) if terminal_error is not None else ""
                runtime_finish = next(
                    (
                        item
                        for item in reversed(getattr(run_state, "runtime_messages", []) or [])
                        if getattr(item, "kind", "") == "run_finished"
                    ),
                    None,
                )
                finish_data = getattr(runtime_finish, "data", {}) if runtime_finish is not None else {}
                if not isinstance(finish_data, dict):
                    finish_data = {}
                # RuntimeState frames carry the detailed projection, but the
                # stream still needs the normal top-level terminal envelope so
                # clients can apply one consistent lifecycle transition.  The
                # delete frame is intentionally queued first by ``finish``.
                enqueue_terminal(
                    {
                        "type": "done" if terminal_status == "success" else "error",
                        "status": terminal_status,
                        "final_answer": final_answer if terminal_status == "success" else rendered_error,
                        "session_id": active_session.session_id if active_session is not None else session_id,
                        "run_id": run_state.run_id
                        if run_state is not None
                        else (current_run.run_id if current_run else None),
                        "mode": getattr(run_state, "mode", None)
                        or (current_run.mode if current_run is not None else mode),
                        "metrics": {
                            "duration_ms": finished.get("duration_ms", finish_data.get("duration_ms")),
                            "model_calls": finished.get("model_calls", finish_data.get("model_calls")),
                            "tool_calls": finished.get("tool_calls", finish_data.get("tool_calls")),
                            "active_skills": finished.get("active_skills", finish_data.get("active_skills", [])),
                        },
                        **(
                            {"error": rendered_error or FAILED_TERMINAL_MESSAGE} if terminal_status != "success" else {}
                        ),
                    }
                )
            else:
                enqueue_terminal(
                    {
                        "type": "done",
                        "status": run_state.status if run_state is not None else "idle",
                        "final_answer": (run_state.final_answer if run_state is not None else "") or "",
                        "session_id": active_session.session_id if active_session is not None else session_id,
                        "run_id": run_state.run_id
                        if run_state is not None
                        else (current_run.run_id if current_run else None),
                        "mode": getattr(run_state, "mode", None)
                        or (current_run.mode if current_run is not None else mode),
                        "metrics": {
                            "duration_ms": finished.get("duration_ms"),
                            "model_calls": finished.get("model_calls"),
                            "tool_calls": finished.get("tool_calls"),
                            "active_skills": finished.get("active_skills", []),
                        },
                    }
                )
            if identity is not None and state.snapshot_manager is not None:
                state.snapshot_manager.notify_run_finished(identity.id)
        except ModelConfigurationError as exc:
            if bridge_ref["bridge"] is not None:
                error_message = f"模型未配置：{exc}"
                bridge_ref["bridge"].finish("abort", error_message, category="agent", code="model_configuration_error")
                rendered_error = terminal_error_text(bridge_ref["bridge"].terminal_error or {})
                enqueue_terminal(
                    {"type": "error", "status": "abort", "error": rendered_error, "message": rendered_error}
                )
            else:
                enqueue_terminal({"type": "error", "message": f"模型未配置：{exc}"})
        except Exception as exc:
            bridge = bridge_ref["bridge"]
            if bridge is not None:
                final_node = bridge.finish_exception(exc)
                terminal_status = final_node.status if final_node is not None else "failed"
                rendered_error = terminal_error_text(bridge.terminal_error or {}) if bridge.terminal_error else ""
                if terminal_status == "failed":
                    rendered_error = rendered_error or FAILED_TERMINAL_MESSAGE
                enqueue_terminal(
                    {
                        "type": "error",
                        "status": terminal_status,
                        "error": rendered_error,
                        "message": rendered_error,
                    }
                )
            else:
                enqueue_terminal(
                    {
                        "type": "error",
                        "status": "failed",
                        "error": FAILED_TERMINAL_MESSAGE,
                        "message": FAILED_TERMINAL_MESSAGE,
                    }
                )
        finally:
            if app is not None:
                try:
                    app.close()
                except Exception:
                    pass
            done.set()

    threading.Thread(target=worker, daemon=True).start()

    async def generator():
        try:
            while True:
                try:
                    item = q.get_nowait()
                except queue.Empty:
                    if done.is_set():
                        break
                    await asyncio.sleep(0.05)
                    continue
                if item is None:
                    break
                yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"
        finally:
            # A closed browser/TUI response is the cancellation signal for the
            # associated runtime.  Normal completion is harmlessly idempotent.
            cancel_requested.set()

    return generator()


@router.post("/chat")
async def chat(
    body: ChatRequest, request: Request, identity: UserIdentity = Depends(require_user)
) -> StreamingResponse:
    state: WebAppState = request.app.state.web
    if not body.prompt.strip():
        raise HTTPException(status_code=400, detail="prompt 不能为空")
    store = _store(state, identity.id)
    resolved_session_id = body.session_id
    if resolved_session_id:
        summary = _require_active(store, resolved_session_id)
        if summary.last_run_status == "running":
            raise HTTPException(status_code=409, detail="会话已有正在运行的任务，请先停止。")
        nodes = getattr(store, "load_nodes", lambda _session_id: [])(resolved_session_id)
        if nodes and not body.source_node_id:
            raise HTTPException(status_code=409, detail="续聊请求必须提交当前最后节点 ID。")
        _validate_source_node(store, resolved_session_id, body.source_node_id)
    else:
        resolved_session_id = store.create_session().session_id
    state.user_paths(identity.id).ensure_session(resolved_session_id)
    return StreamingResponse(
        _stream(
            state,
            body.prompt.strip(),
            identity=identity,
            session_id=resolved_session_id,
            source_node_id=body.source_node_id,
            mode=body.mode,
            interactive=body.interactive,
            permission_mode=body.permission_mode,
            reasoning_effort=body.reasoning_effort,
            user_preferences=state.agent_preferences_for_user(identity.id),
            default_timezone=str(state.agent_config_for_user(identity.id).get("timezone", DEFAULT_TIME_ZONE)),
            model_config=_model_config_snapshot(state, identity.id),
            runtime_config=state.runtime_config_for_user(identity.id),
        ),
        media_type="text/event-stream",
    )


@router.post("/sessions/{session_id}/resume")
async def resume(
    session_id: str,
    body: ResumeRequest,
    request: Request,
    identity: UserIdentity = Depends(require_user),
) -> StreamingResponse:
    """Resume a durable workflow through the same SSE contract as chat."""

    state: WebAppState = request.app.state.web

    store = _store(state, identity.id)
    summary = _require_active(store, session_id)
    if summary.last_run_status == "running":
        raise HTTPException(status_code=409, detail="会话已有正在运行的任务，请先停止。")
    nodes = getattr(store, "load_nodes", lambda _session_id: [])(session_id)
    if nodes and not body.source_node_id:
        raise HTTPException(status_code=409, detail="恢复请求必须提交当前节点 ID。")
    _validate_source_node(store, session_id, body.source_node_id, resume=True)

    def operation(conversation, interrupt, sink, cancel_requested, request_parameters):
        return conversation.resume_session(
            session_id,
            on_event=sink,
            interrupt=interrupt,
            cancel_requested=cancel_requested,
            request_parameters=request_parameters,
        )

    return StreamingResponse(
        _stream(
            state,
            "",
            identity=identity,
            session_id=session_id,
            source_node_id=body.source_node_id,
            mode="agent",
            interactive=True,
            permission_mode=body.permission_mode,
            reasoning_effort=body.reasoning_effort,
            user_preferences=state.agent_preferences_for_user(identity.id),
            model_config=_model_config_snapshot(state, identity.id),
            runtime_config=state.runtime_config_for_user(identity.id),
            default_timezone=str(state.agent_config_for_user(identity.id).get("timezone", DEFAULT_TIME_ZONE)),
            operation=operation,
        ),
        media_type="text/event-stream",
    )
