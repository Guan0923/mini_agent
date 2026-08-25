"""Turn execution stream used by the Turn-centered API."""

from __future__ import annotations

import asyncio
import html
import json
import queue
import threading
from collections.abc import Callable
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.domain import FAILED_TERMINAL_MESSAGE, terminal_error_text
from backend.jobs import AdmissionPolicy, JobLane, JobScopeKind, ThreadJob
from backend.providers import ModelConfig, ModelConfigurationError
from backend.runtime.node_bridge import RuntimeEventNodeBridge
from backend.sandbox import ApprovalStore
from backend.storage.auth.crypto import SecretDecryptionError

from ..auth.types import UserIdentity
from ..session_store import session_store as _store
from ..shared.runtime import build_user_application
from ..state import WebAppState
from .interrupts import make_interactive_interrupt

ReasoningEffort = Literal["low", "medium", "high", "xhigh", "max"]


def _terminal_type_for_status(status: str, category: str | None) -> Literal["success", "failed"]:
    if status == "success" or (status == "paused" and category == "user"):
        return "success"
    return "failed"


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


def _stream(
    state: WebAppState,
    prompt: str,
    *,
    identity: UserIdentity,
    session_id: str,
    turn_id: str,
    thread_id: str,
    adopt_existing: bool = False,
    source_node_id: str | None = None,
    mode: Literal["agent", "plan"] = "agent",
    permission_mode: Literal["read_only", "workspace_write", "full_access"] | None = None,
    reasoning_effort: ReasoningEffort = "medium",
    provider_name: str | None = None,
    model_snapshot: dict[str, object] | None = None,
    user_preferences: str = "",
    model_config: ModelConfig | None = None,
    request_model: RuntimeModelRequest | None = None,
    references: list[dict[str, str]] | None = None,
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

    # Reserve the session before creating the Job object.  Endpoint-level
    # summary checks are advisory; this lock closes the two-window race where
    # both requests otherwise pass validation and create two running leaves.
    stream_locks = getattr(state, "active_runtime_stream_locks", None)
    if (
        not isinstance(stream_locks, dict)
        or not isinstance(stream_locks.get("keys"), set)
        or not hasattr(stream_locks.get("__lock__"), "__enter__")
    ):
        stream_locks = {"__lock__": threading.RLock(), "keys": set()}
        setattr(state, "active_runtime_stream_locks", stream_locks)
    stream_key = (identity.id, thread_id)
    reserved_stream_keys: set[tuple[str, str]] = {stream_key}
    stream_lock = stream_locks["__lock__"]
    with stream_lock:
        if stream_key in stream_locks["keys"]:
            raise HTTPException(status_code=409, detail="当前 Thread 已有 running Turn。")
        stream_locks["keys"].add(stream_key)

    owner_id = identity.id

    def registry_key(active_thread_id: str) -> tuple[str, str]:
        # Session ids are scoped to the authenticated user.  Include the
        # owner in the process-local controller key to prevent cross-user
        # mutation if two stores ever contain the same session id.
        return owner_id, active_thread_id

    q: queue.Queue = queue.Queue()
    done = threading.Event()
    cancel_requested = threading.Event()
    pause_requested = threading.Event()
    active_turn_cancellations = getattr(state, "active_turn_cancellations", None)
    if not isinstance(active_turn_cancellations, dict):
        active_turn_cancellations = {}
        setattr(state, "active_turn_cancellations", active_turn_cancellations)
    cancellation_key = (identity.id, turn_id)
    active_turn_cancellations[cancellation_key] = pause_requested.set
    job_registry = getattr(state, "job_registry", None)
    job_holder: dict[str, ThreadJob | None] = {"job": None}
    bridge_ref: dict[str, RuntimeEventNodeBridge | None] = {"bridge": None}

    def cancellation_requested() -> bool:
        job = job_holder["job"]
        if job is not None and job.is_cancelled():
            cancel_requested.set()
        return cancel_requested.is_set()

    def suspension_requested() -> bool:
        return pause_requested.is_set()

    def sink(item) -> None:
        if cancellation_requested():
            return
        bridge = bridge_ref["bridge"]
        if isinstance(item, dict):
            if bridge is not None:
                bridge.handle_input(item)
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
                "plan",
            }
            if getattr(item, "kind", "") in boundary_kinds:
                locks = getattr(state, "active_runtime_config_locks", None)
                key = registry_key(bridge.thread_id)
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
                if value is bridge and old_key != registry_key(bridge.thread_id):
                    active_runtime_bridges.pop(old_key, None)
                    active_runtime_configs.pop(old_key, None)
            active_runtime_bridges[registry_key(bridge.thread_id)] = bridge
            new_stream_key = registry_key(bridge.thread_id)
            if new_stream_key not in reserved_stream_keys:
                with stream_lock:
                    stream_locks["keys"].add(new_stream_key)
                reserved_stream_keys.add(new_stream_key)

    def enqueue_terminal(terminal_type: str, terminal_id: str, message: str = "") -> None:
        q.put({"type": "terminal", "terminal_type": terminal_type, "terminal_id": terminal_id, "message": message})

    approval_store = ApprovalStore(_store(state, identity.id))
    interrupt = make_interactive_interrupt(
        sink,
        cancel_requested=cancellation_requested,
        owner_id=owner_id,
        approval_store=approval_store,
    )

    effective_reasoning = request_model.reasoning_effort if request_model is not None else reasoning_effort

    def worker() -> None:
        app = None
        try:
            outer_job = job_holder["job"]
            job_parent_id = outer_job.info().id if outer_job is not None else None
            workspace = state.session_workspace(identity.id, session_id)
            bound_project = state.projects(identity.id).session_project(session_id, include_removed=False)
            if bound_project is not None and not bound_project.available:
                raise RuntimeError("项目 cwd 不可访问，请恢复文件夹后重试。")
            selected_model_config = model_config
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
                project_id=bound_project.project_id if bound_project is not None else None,
                job_registry=job_registry,
                job_parent_id=job_parent_id,
                rag_mode="off",
            )
            conversation = app.open_conversation(session_id)
            # The node id is the optimistic-concurrency boundary for the new
            # tree protocol.  Legacy conversations do not expose nodes yet,
            # so validation is delegated to the node store when available.
            node_store = getattr(app, "session_store", None) or getattr(app, "store", None)
            if callable(getattr(node_store, "create_node", None)):
                if getattr(conversation, "active_session", None) is None:
                    conversation.ensure_session(prompt or None)
                active_session = getattr(conversation, "active_session", None)
                if active_session is not None:
                    bridge_ref["bridge"] = RuntimeEventNodeBridge(
                        node_store,
                        session_id=active_session.session_id,
                        prompt=prompt,
                        turn_id=turn_id,
                        thread_id=thread_id or active_session.session_id,
                        source_node_id=source_node_id,
                        adopt_existing=adopt_existing,
                        user=identity.id,
                        provider=getattr(selected_model_config, "provider", "unknown")
                        if selected_model_config
                        else "unknown",
                        provider_name=provider_name
                        or (
                            getattr(model_config, "provider_name", None) or getattr(model_config, "provider", "unknown")
                            if model_config
                            else "unknown"
                        ),
                        model=(
                            str((model_snapshot or {}).get("current_model"))
                            if isinstance(model_snapshot, dict) and model_snapshot.get("current_model")
                            else getattr(selected_model_config, "model", "unknown")
                        )
                        if selected_model_config
                        else "unknown",
                        model_config={
                            "current_model": getattr(selected_model_config, "model", "unknown")
                            if selected_model_config
                            else "unknown",
                            "context_length": getattr(selected_model_config, "context_size", 128000)
                            if selected_model_config
                            else 128000,
                            "output_length": getattr(selected_model_config, "max_tokens", 8192)
                            if selected_model_config
                            else 8192,
                            "reasoning_effort": reasoning_effort,
                            "thinking": "enable",
                            "temperature": 1.0,
                            **(model_snapshot or {}),
                        },
                        permission_mode=permission_mode or "read_only",
                        running_mode=mode,
                        cwd=str(workspace),
                        references=references,
                        emit=lambda frame: q.put(frame.to_dict()),
                    )
                    bridge_ref["bridge"].apply_runtime_config(
                        {
                            "provider_name": provider_name,
                            "model": model_snapshot or {},
                            "permission_mode": permission_mode or "read_only",
                            "running_mode": mode,
                        }
                    )
                    runtime_for_bridge = getattr(conversation, "runtime", None)
                    if runtime_for_bridge is not None:
                        bridge_ref["bridge"].bind_runtime(runtime_for_bridge)
                    if operation is not None:
                        # Resume keeps the immediate bridge start: there is no
                        # new user text and the bridge must adopt the paused
                        # leaf before the stream begins.
                        bridge_ref["bridge"].start()
                    attach_bridge = getattr(conversation, "attach_runtime_node_bridge", None)
                    if callable(attach_bridge):
                        # The SSE sink already forwards every RuntimeEvent to
                        # this bridge and publishes its NodeFrames.  Mark it
                        # external so ConversationService reuses it without
                        # creating a second placeholder/dynamic pair.
                        attach_bridge(bridge_ref["bridge"], events_external=True)
                    active_runtime_bridges[registry_key(bridge_ref["bridge"].thread_id)] = bridge_ref["bridge"]
            if operation is None:
                request_parameters = _model_request_parameters(request_model, effective_reasoning)
                run_state = conversation.run_task(
                    prompt,
                    mode=mode,
                    on_event=sink,
                    interrupt=interrupt,
                    cancel_requested=cancellation_requested,
                    suspend_requested=suspension_requested,
                    request_parameters=request_parameters,
                    references=references or [],
                )
            else:
                run_state = operation(
                    conversation,
                    interrupt,
                    sink,
                    cancellation_requested,
                    suspension_requested,
                    _model_request_parameters(request_model, effective_reasoning),
                )
            active_session = getattr(conversation, "active_session", None)
            bridge = bridge_ref["bridge"]
            if bridge is not None:
                old_status = str(run_state.status if run_state is not None else "abort")
                stop_reason = str(getattr(run_state, "stop_reason", "") or "")
                if old_status in {"completed", "success"}:
                    requested_status = "success"
                    category = None
                elif old_status == "cancelled":
                    requested_status = "paused"
                    category = "user"
                elif bridge.abort_category is not None:
                    requested_status = (
                        "paused" if bridge.abort_category == "network" and bridge.produced_item else "failed"
                    )
                    category = bridge.abort_category
                else:
                    requested_status = "failed"
                    category = bridge.abort_category or "agent"
                final_answer = (run_state.final_answer if run_state is not None else "") or ""
                if category == "network" and not bridge.produced_item:
                    final_node = bridge._current()
                    bridge.closed = True
                    enqueue_terminal("network", turn_id or bridge.turn_id or "unknown")
                    requested_status = "running"
                else:
                    final_node = bridge.finish(
                        requested_status,
                        final_answer,
                        category=category,
                        code=stop_reason or bridge.abort_code,
                    )
                terminal_status = final_node.status if final_node is not None else "failed"
                terminal_id = turn_id or (final_node.id if final_node is not None else bridge.turn_id or "unknown")
                terminal_error = bridge.terminal_error
                rendered_error = terminal_error_text(terminal_error) if terminal_error is not None else ""
                if requested_status == "running":
                    pass
                else:
                    terminal_type = _terminal_type_for_status(terminal_status, category)
                    enqueue_terminal(
                        terminal_type,
                        terminal_id,
                        "" if terminal_type == "success" else rendered_error or FAILED_TERMINAL_MESSAGE,
                    )
            else:
                enqueue_terminal("failed", turn_id or "unknown", "Turn persistence is unavailable.")
            if state.event_sync_manager is not None:
                # Project conversations are local-only.  Their runtime writes
                # are already excluded from the outbox; do not let the
                # Generic end-of-run hook marks the account event stream dirty.
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
                    state.event_sync_manager.notify_run_finished(identity.id)
        except ModelConfigurationError as exc:
            if bridge_ref["bridge"] is not None:
                error_message = f"模型未配置：{exc}"
                bridge_ref["bridge"].finish("failed", error_message, category="agent", code="model_configuration_error")
                rendered_error = terminal_error_text(bridge_ref["bridge"].terminal_error or {})
                enqueue_terminal("failed", turn_id or bridge_ref["bridge"].turn_id or "unknown", rendered_error)
            else:
                enqueue_terminal("failed", turn_id or "unknown", f"模型未配置：{exc}")
        except Exception as exc:
            bridge = bridge_ref["bridge"]
            if bridge is not None:
                final_node = bridge.finish_exception(exc)
                rendered_error = terminal_error_text(bridge.terminal_error or {}) if bridge.terminal_error else ""
                terminal_id = turn_id or (final_node.id if final_node is not None else bridge.turn_id or "unknown")
                if bridge.abort_category == "network" and not bridge.produced_item:
                    enqueue_terminal("network", terminal_id)
                else:
                    enqueue_terminal("failed", terminal_id, rendered_error or str(exc))
            else:
                enqueue_terminal("failed", turn_id or "unknown", str(exc) or FAILED_TERMINAL_MESSAGE)
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
            if reserved_stream_keys:
                with stream_lock:
                    stream_locks["keys"].difference_update(reserved_stream_keys)
            active_turn_cancellations.pop(cancellation_key, None)
            done.set()

    if job_registry is not None:
        parent_scope = getattr(state, "system_job_scope", job_registry.root_scope())
        user_scope = parent_scope.child(
            JobScopeKind.USER,
            user_id=owner_id or None,
            session_id=session_id,
        )
        job = ThreadJob(job_registry.new_job_id(), worker)
        job_holder["job"] = job
        job_registry.submit(job, scope=user_scope, lane=JobLane.FOREGROUND, admission=AdmissionPolicy())
    else:
        # Focused embedding tests may use a minimal state double.  Preserve a
        # self-contained fallback while production always supplies a registry.
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
                if item.get("type") == "terminal":
                    terminal_id = html.escape(str(item.get("terminal_id") or "unknown"), quote=True)
                    terminal_type = html.escape(str(item.get("terminal_type") or "failed"), quote=True)
                    message = html.escape(str(item.get("message") or ""), quote=False)
                    yield f'data: <SSE id="{terminal_id}" type="{terminal_type}">{message}</SSE>\n\n'
                elif item.get("type") in {"turn.create", "turn.update"}:
                    yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"
        finally:
            # A closed browser/TUI response is the cancellation signal for the
            # associated runtime.  Normal completion is harmlessly idempotent.
            cancel_requested.set()
            job = job_holder["job"]
            if job is not None:
                try:
                    job.cancel("stream disconnected")
                except Exception:
                    pass
            if reserved_stream_keys:
                with stream_lock:
                    stream_locks["keys"].difference_update(reserved_stream_keys)

    return generator()
