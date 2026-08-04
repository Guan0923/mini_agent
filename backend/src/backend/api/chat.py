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

from backend.providers import ModelConfigurationError
from backend.runtime import RunnerSettings, build_application
from backend.runtime.core.events import RuntimeEvent

from .auth_dependencies import require_user
from .auth_types import UserIdentity
from .decisions import router as decisions_router
from .interrupts import auto_approve, make_interactive_interrupt
from .sessions import _require_active, _store
from .state import WebAppState

router = APIRouter(prefix="/api")
router.include_router(decisions_router)

ReasoningEffort = Literal["low", "medium", "high", "xhigh", "max"]


class ChatRequest(BaseModel):
    prompt: str
    interactive: bool = False
    session_id: str | None = None
    mode: Literal["agent", "plan"] = "agent"
    permission_mode: Literal["approval_for_me", "full_access"] | None = None
    reasoning_effort: ReasoningEffort = "medium"


class ResumeRequest(BaseModel):
    permission_mode: Literal["approval_for_me", "full_access"] = "approval_for_me"
    reasoning_effort: ReasoningEffort = "medium"


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
        return {**identifiers, "arguments": _truncate(json.dumps(data.get("arguments", {}), ensure_ascii=False), 600)}
    if event.kind == "tool_result":
        return {**identifiers, "tool": data.get("tool"), "result": _truncate(event.message, 800)}
    if event.kind == "tool_failed":
        return {**identifiers, "tool": data.get("tool")}
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
        return {**identifiers, "content": _truncate(event.message, 4000)}
    return identifiers


def _stream(
    state: WebAppState,
    prompt: str,
    interactive: bool = False,
    *,
    identity: UserIdentity | None = None,
    session_id: str | None = None,
    mode: Literal["agent", "plan"] = "agent",
    permission_mode: Literal["approval_for_me", "full_access"] | None = None,
    reasoning_effort: ReasoningEffort = "medium",
    operation: Callable[..., object] | None = None,
):
    q: queue.Queue = queue.Queue()
    done = threading.Event()
    cancel_requested = threading.Event()
    finished: dict = {}

    def sink(item) -> None:
        if cancel_requested.is_set():
            return
        if isinstance(item, dict):
            q.put(item)
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
            workspace = state.user_workspace(identity.id) if identity is not None else state.chat_workspace
            path_options = {"paths": state.user_paths(identity.id)} if identity is not None else {}
            app = build_application(
                workspace,
                planner_name="llm",
                settings=RunnerSettings(log_full_messages=True),
                project_mcp_enabled=False,
                **path_options,
            )
            conversation = app.open_conversation(session_id) if session_id else app.open_conversation()
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
            enqueue_terminal(
                {
                    "type": "done",
                    "status": run_state.status if run_state is not None else "idle",
                    "final_answer": (run_state.final_answer if run_state is not None else "") or "",
                    "session_id": active_session.session_id if active_session is not None else session_id,
                    "run_id": run_state.run_id
                    if run_state is not None
                    else (current_run.run_id if current_run else None),
                    "mode": getattr(run_state, "mode", None) or (current_run.mode if current_run is not None else mode),
                    "metrics": {
                        "duration_ms": finished.get("duration_ms"),
                        "model_calls": finished.get("model_calls"),
                        "tool_calls": finished.get("tool_calls"),
                        "active_skills": finished.get("active_skills", []),
                    },
                }
            )
        except ModelConfigurationError as exc:
            enqueue_terminal({"type": "error", "message": f"模型未配置：{exc}"})
        except Exception as exc:
            enqueue_terminal({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
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
    if body.session_id:
        summary = _require_active(_store(state, identity.id), body.session_id)
        if summary.last_run_status == "running":
            raise HTTPException(status_code=409, detail="会话已有正在运行的任务，请先停止。")
    return StreamingResponse(
        _stream(
            state,
            body.prompt.strip(),
            identity=identity,
            session_id=body.session_id,
            mode=body.mode,
            interactive=body.interactive,
            permission_mode=body.permission_mode,
            reasoning_effort=body.reasoning_effort,
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

    summary = _require_active(_store(state, identity.id), session_id)
    if summary.last_run_status == "running":
        raise HTTPException(status_code=409, detail="会话已有正在运行的任务，请先停止。")

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
            mode="agent",
            interactive=True,
            permission_mode=body.permission_mode,
            reasoning_effort=body.reasoning_effort,
            operation=operation,
        ),
        media_type="text/event-stream",
    )
