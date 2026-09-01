"""Turn execution streaming, process-local coordination, and terminal delivery."""

from __future__ import annotations

import os
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from fastapi import HTTPException

from backend.domain import FAILED_TERMINAL_MESSAGE, ClaimedEnvelope, MessageQueueUnavailable, terminal_error_text
from backend.domain.runtime_state import NodeFrame
from backend.jobs import AdmissionPolicy, JobLane, JobScopeKind, ThreadJob
from backend.providers import ModelConfig, ModelConfigurationError
from backend.runtime.node_bridge import RuntimeEventNodeBridge
from backend.sandbox import ApprovalStore, SandboxInitializationError
from backend.storage.message_queue import RedisAgentMailbox
from backend.storage.settings.crypto import SecretDecryptionError

from ..active_turn_stream import ActiveTurnStream
from ..agent_report_projection import project_frame
from ..pause_control import TurnPauseController
from ..runtime_event_transport import publish_frame as publish_runtime_frame
from ..runtime_event_transport import publish_terminal as publish_runtime_terminal
from ..session_store import session_store as _store
from ..state import WebAppState
from .interrupts import make_interactive_interrupt
from .models import ReasoningEffort, RuntimeModelRequest, _model_request_parameters
from .titles import _auto_title_main_thread


def _terminal_type_for_status(status: str, category: str | None) -> Literal["success", "failed"]:
    if status == "success" or (status == "paused" and category == "user"):
        return "success"
    return "failed"


def _startup_failure_message(error: Exception) -> str:
    """Render failures raised before a persisted Turn baseline exists."""

    if isinstance(error, SandboxInitializationError):
        detail = str(error).strip() or "Windows Sandbox Broker 无法初始化。"
        return f"Sandbox 初始化失败：{detail} Agent 已停止，未降级执行。"
    return str(error) or FAILED_TERMINAL_MESSAGE


def _runtime_stream_lock_registry(state: object) -> dict[str, Any]:
    """Return the process-local registry serializing work within one Thread."""

    stream_locks = getattr(state, "active_runtime_stream_locks", None)
    if (
        not isinstance(stream_locks, dict)
        or not isinstance(stream_locks.get("keys"), set)
        or not hasattr(stream_locks.get("__lock__"), "__enter__")
    ):
        stream_locks = {"__lock__": threading.RLock(), "keys": set()}
        setattr(state, "active_runtime_stream_locks", stream_locks)
    return stream_locks


def _stream(
    state: WebAppState,
    prompt: str,
    *,
    application_builder: Callable[..., Any],
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
    initial_delivery: ClaimedEnvelope | None = None,
    subscribe: bool = True,
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
    stream_locks = _runtime_stream_lock_registry(state)
    stream_key = thread_id
    reserved_stream_keys: set[str] = {stream_key}
    stream_lock = stream_locks["__lock__"]
    with stream_lock:
        if stream_key in stream_locks["keys"]:
            raise HTTPException(status_code=409, detail="当前 Thread 已有 running Turn。")
        stream_locks["keys"].add(stream_key)

    def registry_key(active_thread_id: str) -> str:
        return active_thread_id

    active_turn_streams = getattr(state, "active_turn_streams", None)
    if not isinstance(active_turn_streams, dict):
        active_turn_streams = {}
        setattr(state, "active_turn_streams", active_turn_streams)
    active_turn_streams_lock = getattr(state, "active_turn_streams_lock", None)
    if not hasattr(active_turn_streams_lock, "__enter__"):
        active_turn_streams_lock = threading.RLock()
        setattr(state, "active_turn_streams_lock", active_turn_streams_lock)
    stream_store = _store(state)
    active_stream = ActiveTurnStream(
        turn_id,
        lambda frame, current: project_frame(stream_store, frame, current),
    )
    active_stream_aliases: set[str] = {turn_id}
    with active_turn_streams_lock:
        active_turn_streams[turn_id] = active_stream
    original_subscription = active_stream.subscribe(turn_id) if subscribe else None
    cancel_requested = threading.Event()
    pause_controller = TurnPauseController()
    active_turn_cancellations = getattr(state, "active_turn_cancellations", None)
    if not isinstance(active_turn_cancellations, dict):
        active_turn_cancellations = {}
        setattr(state, "active_turn_cancellations", active_turn_cancellations)
    cancellation_key = turn_id
    active_turn_cancellations[cancellation_key] = pause_controller
    steering_inbox = RedisAgentMailbox(
        state.message_queue,
        turn_id,
        thread_id,
        f"backend-{os.getpid()}-{threading.get_ident()}",
    )
    job_registry = getattr(state, "job_registry", None)
    job_holder: dict[str, ThreadJob | None] = {"job": None}
    bridge_ref: dict[str, RuntimeEventNodeBridge | None] = {"bridge": None}

    def cancellation_requested() -> bool:
        job = job_holder["job"]
        if job is not None and job.is_cancelled():
            cancel_requested.set()
        return cancel_requested.is_set()

    def suspension_requested() -> bool:
        return pause_controller.is_requested()

    def sink(item) -> None:
        if cancellation_requested():
            return
        if pause_controller.is_requested() and getattr(item, "kind", "") not in {"cancelled", "run_suspended"}:
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

    def publish_frame(frame: NodeFrame) -> None:
        bridge = bridge_ref["bridge"]
        if bridge is None:
            return
        alias = frame.turn_id
        with active_turn_streams_lock:
            active_turn_streams[alias] = active_stream
            active_stream_aliases.add(alias)
        current = bridge.writer.current(frame.session_id, frame.turn_id)
        publish_runtime_frame(state, frame, current)
        active_stream.publish_frame(frame, current)

    def enqueue_terminal(terminal_type: str, terminal_id: str, message: str = "") -> None:
        active_stream.publish_terminal(terminal_type, terminal_id, message)
        bridge = bridge_ref["bridge"]
        current = bridge._current() if bridge is not None else None
        publish_runtime_terminal(
            state,
            session_id=current.session_id if current is not None else session_id,
            thread_id=current.thread_id if current is not None else thread_id,
            turn_id=terminal_id,
            terminal_type=terminal_type,
            message=message,
        )

    approval_store = ApprovalStore(_store(state))
    interrupt = make_interactive_interrupt(
        sink,
        cancel_requested=cancellation_requested,
        approval_store=approval_store,
    )

    effective_reasoning = request_model.reasoning_effort if request_model is not None else reasoning_effort

    def worker() -> None:
        app = None
        conversation = None
        try:
            outer_job = job_holder["job"]
            job_parent_id = outer_job.info().id if outer_job is not None else None
            state.paths.ensure_session(session_id)
            workspace = state.paths.session_workspace(session_id)
            bound_project = state.projects.session_project(session_id, include_removed=False)
            if bound_project is not None and not bound_project.available:
                raise RuntimeError("项目 cwd 不可访问，请恢复文件夹后重试。")
            project_cwd = Path(bound_project.cwd).resolve() if bound_project is not None else None
            selected_model_config = model_config
            if provider_name:
                try:
                    selected_model_config = state.model_config(provider_name)
                except (SecretDecryptionError, ModelConfigurationError) as exc:
                    raise ModelConfigurationError(str(exc)) from exc
            app = application_builder(
                state,
                session_id=session_id,
                user_preferences=user_preferences,
                model_config=selected_model_config,
                load_model_config=False,
                workspace=workspace,
                project_id=bound_project.project_id if bound_project is not None else None,
                project_cwd=project_cwd,
                job_registry=job_registry,
                job_parent_id=job_parent_id,
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
                        user="",
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
                            "temperature": getattr(selected_model_config, "temperature", 0.0)
                            if selected_model_config
                            else 0.0,
                            **(model_snapshot or {}),
                        },
                        permission_mode=permission_mode or "read_only",
                        running_mode=mode,
                        cwd=str(workspace),
                        project_cwd=str(project_cwd) if project_cwd is not None else "",
                        references=references,
                        delivery_id=initial_delivery.envelope.delivery_id if initial_delivery is not None else None,
                        emit=publish_frame,
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
                        runtime_for_bridge.services.steering = steering_inbox.take
                        runtime_for_bridge.services.register_operation_abort = pause_controller.register_abort
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
                    steering=steering_inbox.take,
                    delivery_id=initial_delivery.envelope.delivery_id if initial_delivery is not None else None,
                    on_started=(
                        lambda: state.message_queue.ack(initial_delivery) if initial_delivery is not None else None
                    ),
                )
            else:
                run_state = operation(
                    conversation,
                    interrupt,
                    sink,
                    cancellation_requested,
                    suspension_requested,
                    _model_request_parameters(request_model, effective_reasoning),
                    steering_inbox.take,
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
                terminal_id = final_node.id if final_node is not None else turn_id or bridge.turn_id or "unknown"
                terminal_error = bridge.terminal_error
                rendered_error = terminal_error_text(terminal_error) if terminal_error is not None else ""
                if requested_status == "running":
                    pass
                else:
                    if conversation is not None and final_node is not None:
                        _auto_title_main_thread(
                            conversation,
                            node_store,
                            session_id=final_node.session_id,
                            thread_id=final_node.thread_id,
                            turn_id=final_node.id,
                        )
                    terminal_type = _terminal_type_for_status(terminal_status, category)
                    enqueue_terminal(
                        terminal_type,
                        terminal_id,
                        "" if terminal_type == "success" else rendered_error or FAILED_TERMINAL_MESSAGE,
                    )
            else:
                enqueue_terminal("failed", turn_id or "unknown", "Turn persistence is unavailable.")
        except MessageQueueUnavailable:
            bridge = bridge_ref["bridge"]
            if bridge is not None:
                final_node = bridge.finish(
                    "failed",
                    "消息队列连接中断，Turn 已失败。",
                    category="server",
                    code="message_queue_unavailable",
                )
                terminal_id = final_node.id if final_node is not None else turn_id or "unknown"
                enqueue_terminal("failed", terminal_id, "message_queue_unavailable")
            else:
                enqueue_terminal("failed", turn_id or "unknown", "message_queue_unavailable")
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
                terminal_id = final_node.id if final_node is not None else turn_id or bridge.turn_id or "unknown"
                if bridge.abort_category == "network" and not bridge.produced_item:
                    enqueue_terminal("network", terminal_id)
                else:
                    enqueue_terminal("failed", terminal_id, rendered_error or str(exc))
            else:
                enqueue_terminal("failed", turn_id or "unknown", _startup_failure_message(exc))
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
            steering_inbox.close()
            with active_turn_streams_lock:
                for alias in active_stream_aliases:
                    if active_turn_streams.get(alias) is active_stream:
                        active_turn_streams.pop(alias, None)
            final = bridge._current() if bridge is not None else None
            if final is not None and final.status in {"success", "failed"}:
                try:
                    state.message_queue.release_turn(final.id)
                except MessageQueueUnavailable:
                    pass

    if job_registry is not None:
        parent_scope = getattr(state, "system_job_scope", job_registry.root_scope())
        session_scope = parent_scope.child(
            JobScopeKind.SESSION,
            session_id=session_id,
        )
        thread_scope = session_scope.child(JobScopeKind.THREAD, thread_id=thread_id)
        job = ThreadJob(job_registry.new_job_id(), worker)
        job_holder["job"] = job
        job_registry.submit(job, scope=thread_scope, lane=JobLane.FOREGROUND, admission=AdmissionPolicy())
    else:
        # Focused embedding tests may use a minimal state double.  Preserve a
        # self-contained fallback while production always supplies a registry.
        threading.Thread(target=worker, daemon=True).start()

    return original_subscription.as_sse() if original_subscription is not None else None
