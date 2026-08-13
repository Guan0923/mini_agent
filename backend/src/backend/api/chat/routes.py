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
from pydantic import BaseModel, ConfigDict, Field, model_validator

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


class RuntimeModelRequest(BaseModel):
    """Complete provider-neutral model settings captured at a request boundary."""

    model_config = ConfigDict(extra="forbid")

    reasoning_effort: ReasoningEffort
    current_model: str = Field(min_length=1, max_length=500)
    context_length: int = Field(gt=1)
    output_length: int = Field(ge=1)
    thinking: Literal["enable", "disable"]
    temperature: float = Field(ge=0, le=2)

    @model_validator(mode="after")
    def validate_limits(self) -> RuntimeModelRequest:
        if self.context_length <= self.output_length:
            raise ValueError("model.context_length must be greater than model.output_length")
        return self


class ChatRequest(BaseModel):
    prompt: str
    interactive: bool = False
    session_id: str | None = None
    mode: Literal["agent", "plan"] = "agent"
    running_mode: Literal["agent", "plan"] | None = None
    permission_mode: Literal["approval_for_me", "full_access"] | None = None
    reasoning_effort: ReasoningEffort = "medium"
    provider_name: str | None = Field(default=None, min_length=1, max_length=80)
    model: RuntimeModelRequest | None = None
    source_node_id: str | None = None

    @model_validator(mode="after")
    def normalize_running_mode(self) -> ChatRequest:
        if self.running_mode is None:
            # ``mode`` is the pre-v0.3 spelling.  Normalize it at the API
            # boundary so old clients remain readable while new clients send
            # both explicit keys.
            # Keep ``running_mode`` unset, however.  The route must be able
            # to distinguish a legacy/default ``mode`` from an explicit
            # v0.3 ``running_mode`` when an existing message tree is resumed.
            # ``mode`` remains the effective value for the runtime below.
            return self
        elif self.mode != "agent" and self.mode != self.running_mode:
            raise ValueError("mode and running_mode must match")
        self.mode = self.running_mode
        return self


class ResumeRequest(BaseModel):
    permission_mode: Literal["approval_for_me", "full_access"] | None = None
    reasoning_effort: ReasoningEffort = "medium"
    source_node_id: str | None = None
    mode: Literal["agent", "plan"] = "agent"
    running_mode: Literal["agent", "plan"] | None = None
    provider_name: str | None = Field(default=None, min_length=1, max_length=80)
    model: RuntimeModelRequest | None = None

    @model_validator(mode="after")
    def normalize_running_mode(self) -> ResumeRequest:
        if self.running_mode is None:
            # Do not materialize the compatibility ``mode`` default into the
            # v0.3 field: the endpoint needs to reject resumed requests that
            # did not explicitly submit ``running_mode``.
            return self
        elif self.mode != "agent" and self.mode != self.running_mode:
            raise ValueError("mode and running_mode must match")
        self.mode = self.running_mode
        return self


def _reasoning_parameters(effort: ReasoningEffort) -> dict[str, object]:
    return {"thinking": {"type": "enabled"}, "reasoning_effort": effort}


def _model_request_parameters(model: RuntimeModelRequest | None, fallback: ReasoningEffort) -> dict[str, object]:
    """Translate the complete public model object into provider request options."""

    if model is None:
        return _reasoning_parameters(fallback)
    if model.thinking == "disable":
        return {
            "thinking": {"type": "disabled"},
            "max_tokens": model.output_length,
            "temperature": model.temperature,
        }
    return {
        "thinking": {"type": "enabled"},
        "reasoning_effort": model.reasoning_effort,
        "max_tokens": model.output_length,
        "temperature": model.temperature,
    }


def _require_explicit_runtime_config(
    *,
    provider_name: str | None,
    model: RuntimeModelRequest | None,
    permission_mode: str | None,
    running_mode: str | None,
    permission_mode_explicit: bool = True,
    running_mode_explicit: bool = True,
    resume: bool = False,
) -> None:
    """Reject an established/resumed request that cannot define its boundary.

    A brand-new empty chat can still be initialized from the user's active
    provider settings.  Once a message-tree exists, however, the request must
    carry the complete runtime selection so a worker never silently combines
    stale browser state with a new node.
    """

    missing: list[str] = []
    if not provider_name:
        missing.append("provider_name")
    if model is None:
        missing.append("model")
    if permission_mode is None or not permission_mode_explicit:
        missing.append("permission_mode")
    if running_mode is None or not running_mode_explicit:
        missing.append("running_mode")
    if missing:
        kind = "Resume" if resume else "Chat"
        raise HTTPException(
            status_code=422,
            detail=f"{kind} 请求必须显式提交完整运行配置：{', '.join(missing)}",
        )


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
    provider_name: str | None = None,
    model_snapshot: dict[str, object] | None = None,
    user_preferences: str = "",
    default_timezone: str = DEFAULT_TIME_ZONE,
    model_config: ModelConfig | None = None,
    runtime_config: dict[str, object] | None = None,
    request_model: RuntimeModelRequest | None = None,
    operation: Callable[..., object] | None = None,
):
    # ``WebAppState`` owns these process-local registries in production, but
    # callers such as focused SSE tests may provide a small state double.  A
    # stream must remain self-contained in that case instead of failing in
    # the worker after the response has already been opened.
    active_runtime_configs = getattr(state, "active_runtime_configs", None)
    if not isinstance(active_runtime_configs, dict):
        active_runtime_configs = {}
        setattr(state, "active_runtime_configs", active_runtime_configs)
    active_runtime_bridges = getattr(state, "active_runtime_bridges", None)
    if not isinstance(active_runtime_bridges, dict):
        active_runtime_bridges = {}
        setattr(state, "active_runtime_bridges", active_runtime_bridges)

    owner_id = identity.id if identity is not None else ""

    def registry_key(active_session_id: str) -> tuple[str, str]:
        # Session ids are scoped to the authenticated user.  Include the
        # owner in the process-local controller key to prevent cross-user
        # mutation if two stores ever contain the same session id.
        return owner_id, active_session_id

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
            # A PATCH updates the dynamic node immediately, but its runtime
            # configuration is consumed only at an execution boundary.  Do
            # not drain the process-local compatibility registry for ordinary
            # stream events (response/thinking deltas): doing so would let a
            # concurrent UI change alter the request that is already in
            # flight.  ModelRequestExecutor and ToolStepExecutor are the
            # authoritative consumers; this fallback is only for embedding
            # callers that have not yet bound an AgentRuntime.
            boundary_kinds = {
                "model_request",
                "tool_call",
                "strategy",
                "plan",
                "replan_requested",
                "evaluate",
            }
            if getattr(item, "kind", "") in boundary_kinds:
                locks = getattr(state, "active_runtime_config_locks", None)
                key = registry_key(bridge.session_id)
                lock = locks.setdefault(key, threading.RLock()) if isinstance(locks, dict) else None
                if lock is None:
                    pending = active_runtime_configs.get(key)
                    if pending:
                        bridge.apply_runtime_config(pending)
                        active_runtime_configs.pop(key, None)
                else:
                    with lock:
                        pending = active_runtime_configs.get(key)
                        if pending:
                            bridge.apply_runtime_config(pending)
                            active_runtime_configs.pop(key, None)
            bridge.handle(item)
            # ``RuntimeEventNodeBridge`` may switch sessions during a plan
            # handoff.  Re-key the controller immediately so the new active
            # leaf remains PATCH-addressable while the same stream continues.
            for old_key, value in list(active_runtime_bridges.items()):
                if value is bridge and old_key != registry_key(bridge.session_id):
                    active_runtime_bridges.pop(old_key, None)
                    active_runtime_configs.pop(old_key, None)
            active_runtime_bridges[registry_key(bridge.session_id)] = bridge
            return
        payload = _event_payload(item)
        if item.kind == "run_finished":
            finished.update(payload)
        q.put({"type": "event", "kind": item.kind, "message": item.message, "data": payload})

    def enqueue_terminal(item: dict) -> None:
        if not cancel_requested.is_set():
            q.put(item)

    owner_id = identity.id if identity is not None else None
    interactive_interrupt = make_interactive_interrupt(
        sink,
        cancel_requested=cancel_requested.is_set,
        owner_id=owner_id,
    )
    full_access_interrupt = make_interactive_interrupt(
        sink,
        cancel_requested=cancel_requested.is_set,
        auto_approve_tools=True,
        owner_id=owner_id,
    )

    effective_reasoning = (
        request_model.reasoning_effort
        if request_model is not None
        else reasoning_effort
    )
    if permission_mode == "full_access":
        interrupt = full_access_interrupt
    elif interactive or permission_mode == "approval_for_me":
        interrupt = interactive_interrupt
    else:
        interrupt = auto_approve

    # The original interrupt closure is kept for non-tool questions, but tool
    # approval is selected from the bridge's latest permission at each call.
    # This makes a running approval/full-access switch effective immediately
    # without changing an already-approved invocation.
    base_interrupt = interrupt

    def live_interrupt(request):
        if request.kind == "tool":
            bridge = bridge_ref["bridge"]
            if bridge is not None:
                if getattr(bridge, "permission_mode", "approval_for_me") == "full_access":
                    return full_access_interrupt(request)
                return interactive_interrupt(request)
        return base_interrupt(request)

    interrupt = live_interrupt

    def worker() -> None:
        app = None
        try:
            workspace = (
                state.session_workspace(identity.id, session_id)
                if identity is not None and session_id is not None
                else state.chat_workspace
            )
            path_options = {"paths": state.user_paths(identity.id)} if identity is not None else {}
            selected_model_config = model_config
            if identity is not None:
                if provider_name:
                    try:
                        selected_model_config = state.model_config_for_provider_name(identity.id, provider_name)
                    except (SecretDecryptionError, ModelConfigurationError) as exc:
                        raise ModelConfigurationError(str(exc)) from exc
                app = build_user_application(
                    state,
                    identity.id,
                    session_id=session_id,
                    user_preferences=user_preferences,
                    model_config=selected_model_config,
                    load_model_config=False,
                    workspace=workspace,
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
                        user=identity.id if identity is not None else "",
                        provider=getattr(selected_model_config, "provider", "unknown") if selected_model_config else "unknown",
                        provider_name=provider_name or (getattr(model_config, "provider_name", None) or getattr(model_config, "provider", "unknown") if model_config else "unknown"),
                        model=(str((model_snapshot or {}).get("current_model")) if isinstance(model_snapshot, dict) and model_snapshot.get("current_model") else getattr(selected_model_config, "model", "unknown")) if selected_model_config else "unknown",
                        model_config={
                            "current_model": getattr(selected_model_config, "model", "unknown") if selected_model_config else "unknown",
                            "context_length": getattr(selected_model_config, "context_size", 128000) if selected_model_config else 128000,
                            "output_length": getattr(selected_model_config, "max_tokens", 8192) if selected_model_config else 8192,
                            "reasoning_effort": reasoning_effort,
                            "thinking": "enable",
                            "temperature": 1.0,
                            **(model_snapshot or {}),
                        },
                        permission_mode=permission_mode or "approval_for_me",
                        running_mode=mode,
                        cwd=str(workspace),
                        emit=lambda frame: q.put(frame.to_dict()) if not cancel_requested.is_set() else None,
                    )
                    bridge_ref["bridge"].apply_runtime_config(
                        {
                            "provider_name": provider_name,
                            "model": model_snapshot or {},
                            "permission_mode": permission_mode or "approval_for_me",
                            "running_mode": mode,
                        }
                    )
                    runtime_for_bridge = getattr(conversation, "runtime", None)
                    if runtime_for_bridge is not None:
                        bridge_ref["bridge"].bind_runtime(runtime_for_bridge)
                    bridge_ref["bridge"].start()
                    attach_bridge = getattr(conversation, "attach_runtime_node_bridge", None)
                    if callable(attach_bridge):
                        # The SSE sink already forwards every RuntimeEvent to
                        # this bridge and publishes its NodeFrames.  Mark it
                        # external so ConversationService reuses it without
                        # creating a second placeholder/dynamic pair.
                        attach_bridge(bridge_ref["bridge"], events_external=True)
                    active_runtime_bridges[registry_key(active_session.session_id)] = bridge_ref["bridge"]
            if operation is None:
                run_state = conversation.run_task(
                    prompt,
                    mode=mode,
                    on_event=sink,
                    interrupt=interrupt,
                    cancel_requested=cancel_requested.is_set,
                    request_parameters=_model_request_parameters(request_model, effective_reasoning),
                )
            else:
                run_state = operation(
                    conversation,
                    interrupt,
                    sink,
                    cancel_requested.is_set,
                    _model_request_parameters(request_model, effective_reasoning),
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
                terminal_status = (
                    final_node.status
                    if final_node is not None
                    else "failed"
                    if getattr(bridge, "persistence_failed", False)
                    else requested_status
                )
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
                # Project conversations are local-only.  Their runtime writes
                # are already excluded from the outbox; do not let the
                # generic end-of-run hook mark the account cloud snapshot
                # dirty either.
                local_only = bool(getattr(active_session, "local_only", False))
                if not local_only and session_id:
                    session_store = getattr(app, "session_store", None)
                    getter = getattr(session_store, "get_session", None)
                    if callable(getter):
                        try:
                            persisted = getter(session_id)
                            local_only = bool(getattr(persisted, "local_only", False))
                        except Exception:
                            pass
                if not local_only:
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
            bridge = bridge_ref["bridge"]
            if bridge is not None:
                # A plan handoff can switch the bridge to a new session while
                # the old key remains in the process-local registry.  Remove
                # every alias that points to this bridge, otherwise a later
                # PATCH could mutate a completed run.
                for key, value in list(active_runtime_bridges.items()):
                    if value is bridge:
                        active_runtime_bridges.pop(key, None)
                        active_runtime_configs.pop(key, None)
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
        try:
            state.session_workspace(identity.id, resolved_session_id)
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if summary.last_run_status == "running":
            raise HTTPException(status_code=409, detail="会话已有正在运行的任务，请先停止。")
        nodes = getattr(store, "load_nodes", lambda _session_id: [])(resolved_session_id)
        if nodes:
            _require_explicit_runtime_config(
                provider_name=body.provider_name,
                model=body.model,
                permission_mode=body.permission_mode,
                running_mode=body.running_mode,
                permission_mode_explicit="permission_mode" in body.model_fields_set,
                running_mode_explicit="running_mode" in body.model_fields_set,
            )
        if nodes and not body.source_node_id:
            raise HTTPException(status_code=409, detail="续聊请求必须提交当前最后节点 ID。")
        _validate_source_node(store, resolved_session_id, body.source_node_id)
    else:
        # A project conversation must always be created through the scoped
        # project endpoint.  The chat endpoint's implicit session is only for
        # ordinary, isolated conversations and is therefore safe to sync.
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
            provider_name=body.provider_name,
            model_snapshot=body.model.model_dump() if body.model is not None else None,
            request_model=body.model,
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
    try:
        state.session_workspace(identity.id, session_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if summary.last_run_status == "running":
        raise HTTPException(status_code=409, detail="会话已有正在运行的任务，请先停止。")
    nodes = getattr(store, "load_nodes", lambda _session_id: [])(session_id)
    _require_explicit_runtime_config(
        provider_name=body.provider_name,
        model=body.model,
        permission_mode=body.permission_mode,
        running_mode=body.running_mode,
        permission_mode_explicit="permission_mode" in body.model_fields_set,
        running_mode_explicit="running_mode" in body.model_fields_set,
        resume=True,
    )
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
            mode=body.mode,
            interactive=True,
            permission_mode=body.permission_mode,
            reasoning_effort=body.reasoning_effort,
            provider_name=body.provider_name,
            model_snapshot=body.model.model_dump() if body.model is not None else None,
            request_model=body.model,
            user_preferences=state.agent_preferences_for_user(identity.id),
            model_config=_model_config_snapshot(state, identity.id),
            runtime_config=state.runtime_config_for_user(identity.id),
            default_timezone=str(state.agent_config_for_user(identity.id).get("timezone", DEFAULT_TIME_ZONE)),
            operation=operation,
        ),
        media_type="text/event-stream",
    )
