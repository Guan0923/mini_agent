"""Subagent Turn admission, execution, recovery, and Runtime bridge lifecycle."""

from __future__ import annotations

from collections.abc import Mapping
from uuid import uuid4

from backend.domain import (
    CHECKPOINT_PREAMBLE,
    SystemMessage,
    ThreadNode,
)
from backend.domain.runtime_state import (
    NodeFrame,
    RuntimeState,
    new_node_id,
    runtime_node_from_dict,
    terminal_error_payload,
    utc_iso,
)
from backend.jobs import AdmissionPolicy, JobLane, JobScopeKind, ThreadJob
from backend.tools import ToolError

from ..core.context import AgentRuntime
from ..core.context.exchange import _chat_messages_from_nodes
from ..core.contracts import InterruptRequest
from ..core.events import RuntimeEvent
from ..node_bridge import RuntimeEventNodeBridge
from ..persistence.recording import persistent_event
from .contracts import ChildRunner, _CanonicalRuntimeStore, _SessionBinding
from .tool_executor import LockedToolExecutor


class _SubagentExecutionMixin:
    """Own background Turn workers while SQLite remains canonical."""

    def apply_runtime_config(
        self,
        session_id: str,
        thread_id: str,
        changes: Mapping[str, object],
    ) -> RuntimeState | None:
        target = getattr(self._store, "get_thread_node")(session_id, thread_id) if self._store is not None else None
        if target is None or target.depth <= 0:
            return None
        with self._state_lock:
            bridge = self._active_bridges.get(thread_id)
        return bridge.apply_runtime_config(changes) if bridge is not None else None

    def _wake_thread(self, session_id: str, thread_id: str) -> dict[str, object]:
        target = getattr(self._store, "get_thread_node")(session_id, thread_id)
        runtime_thread = getattr(self._store, "get_runtime_thread")(session_id, thread_id)
        if target is None or runtime_thread is None:
            return {"target_state": "missing"}
        if runtime_thread.running_turn_id:
            return {"target_state": "running", "turn_id": runtime_thread.running_turn_id}
        envelope = getattr(self._queue, "peek_thread")(thread_id)
        if envelope is None:
            return {"target_state": "idle"}
        head_id = runtime_thread.current_turn_id
        parent = getattr(self._store, "get_node")(session_id, head_id) if head_id else None
        if not isinstance(parent, RuntimeState):
            return {"target_state": "idle", "background_admission": "missing_parent"}
        if parent.status == "paused":
            resumed = getattr(self._store, "resume_turn_node")(parent.id)
            admission = self._submit_turn(
                target,
                resumed,
                creator_thread_id=envelope.source_thread_id,
            )
            return {"target_state": "started", "turn_id": resumed.id, "background_admission": admission}
        if parent.status not in {"success", "failed"}:
            return {"target_state": parent.status, "turn_id": parent.id}
        turn_id = new_node_id()
        item = {"type": "text", "text": envelope.content, "status": "success"}
        if envelope.references:
            item["references"] = [dict(reference) for reference in envelope.references]
        requested_config = envelope.payload.get("runtime_config")
        runtime_config = dict(requested_config) if isinstance(requested_config, Mapping) else {}
        requested_model = runtime_config.get("model")
        model = {**parent.model, **dict(requested_model)} if isinstance(requested_model, Mapping) else parent.model
        node = RuntimeState.create(
            session_id=session_id,
            thread_id=thread_id,
            id=turn_id,
            parent=parent,
            user_content=[item],
            provider_name=str(runtime_config.get("provider_name") or parent.provider_name),
            model=model,
            permission_mode=str(runtime_config.get("permission_mode") or parent.permission_mode),
            running_mode=str(runtime_config.get("running_mode") or parent.running_mode),
            cwd=parent.cwd,
            project_cwd=parent.project_cwd,
        )
        node.data[0][0]["delivery_id"] = envelope.delivery_id
        node = RuntimeState.from_dict(node.to_dict())
        try:
            getattr(self._store, "create_thread_turn_if_idle")(
                node,
                expected_head_id=head_id,
                report_recipient_thread_id=envelope.source_thread_id
                if bool(envelope.payload.get("need_reply", False))
                else None,
            )
        except ValueError:
            current = getattr(self._store, "get_runtime_thread")(session_id, thread_id)
            return {
                "target_state": "running" if current and current.running_turn_id else "idle",
                "turn_id": current.running_turn_id if current else None,
            }
        admission = self._submit_turn(
            target,
            node,
            creator_thread_id=envelope.source_thread_id,
            initial_delivery_id=envelope.delivery_id,
        )
        return {"target_state": "started", "turn_id": turn_id, "background_admission": admission}

    def _submit_turn(
        self,
        node: ThreadNode,
        turn: RuntimeState,
        *,
        creator_thread_id: str,
        initial_delivery_id: str | None = None,
        recover_delivery: bool = False,
    ) -> str:
        if self._thread_events is not None:
            self._thread_events.start_turn(turn)
        binding = self._binding(node.session_id)
        if binding is None:
            self._fail_preallocated(turn, "No Agent runner is bound for this Session.")
            return "rejected:no_runner"

        def worker() -> None:
            with self._worker_slots:
                try:
                    self._execute_turn(
                        binding,
                        node,
                        turn,
                        creator_thread_id=creator_thread_id,
                        initial_delivery_id=initial_delivery_id,
                        recover_delivery=recover_delivery,
                    )
                finally:
                    with self._state_lock:
                        self._jobs.pop(node.thread_id, None)
                        control = self._status_controls.pop(node.thread_id, None)
                        if control is not None:
                            control.settled.set()
                    current_thread = getattr(self._store, "get_runtime_thread")(node.session_id, node.thread_id)
                    next_turn_id = current_thread.running_turn_id if current_thread is not None else None
                    if next_turn_id and next_turn_id != turn.id:
                        next_turn = getattr(self._store, "get_node")(node.session_id, next_turn_id)
                        next_node = getattr(self._store, "get_thread_node")(node.session_id, node.thread_id)
                        pending = getattr(self._queue, "peek_thread")(node.thread_id)
                        if isinstance(next_turn, RuntimeState) and next_node is not None:
                            delivery_id = str(next_turn.user_message.get("delivery_id") or "")
                            self._submit_turn(
                                next_node,
                                next_turn,
                                creator_thread_id=(
                                    pending.source_thread_id if pending is not None else creator_thread_id
                                ),
                                initial_delivery_id=delivery_id or None,
                            )

        with self._state_lock:
            existing = self._jobs.get(node.thread_id)
            if existing is not None and str(existing.info().state) not in {"succeeded", "failed", "cancelled"}:
                return "already_running"
            job = ThreadJob(getattr(self._job_registry, "new_job_id")(), worker)
            self._jobs[node.thread_id] = job
        try:
            root = getattr(self._job_registry, "root_scope")()
            scope = root.child(JobScopeKind.SESSION, session_id=node.session_id).child(
                JobScopeKind.THREAD,
                thread_id=node.thread_id,
            )
            getattr(self._job_registry, "submit")(
                job,
                scope=scope,
                lane=JobLane.FOREGROUND,
                admission=AdmissionPolicy(),
            )
        except Exception as exc:
            with self._state_lock:
                self._jobs.pop(node.thread_id, None)
            self._fail_preallocated(turn, self._safe_error(exc))
            return f"rejected:{exc.__class__.__name__}"
        return "admitted"

    def _binding(self, session_id: str) -> _SessionBinding | None:
        with self._state_lock:
            return self._bindings.get(session_id) or self._bindings.get("*")

    def _execute_turn(
        self,
        binding: _SessionBinding,
        node: ThreadNode,
        turn: RuntimeState,
        *,
        creator_thread_id: str,
        initial_delivery_id: str | None,
        recover_delivery: bool,
    ) -> None:
        stored_turn = getattr(self._store, "get_node")(node.session_id, turn.id)
        if isinstance(stored_turn, RuntimeState):
            turn = stored_turn
        runner: ChildRunner | None = None
        mailbox = None

        def emit(frame: NodeFrame) -> None:
            if self._thread_events is None:
                return
            self._thread_events.publish_frame(
                node.thread_id,
                frame,
                bridge.writer.current(frame.session_id, frame.turn_id),
            )

        try:
            runner = binding.runner_factory()
            runner.subagents = self
            runner.tools = LockedToolExecutor(
                runner.tools,
                self._locks,
                binding.workspace,
                binding.project_workspace,
            )
            bridge = RuntimeEventNodeBridge(
                self._store,
                session_id=node.session_id,
                thread_id=node.thread_id,
                source_node_id=turn.id,
                adopt_existing=True,
                prompt="",
                provider_name=turn.provider_name,
                model=str(turn.model["current_model"]),
                model_config=turn.model,
                permission_mode=turn.permission_mode,
                running_mode=turn.running_mode,
                cwd=turn.cwd,
                project_cwd=turn.project_cwd,
                isolated_thread_context=True,
                emit=emit,
            )
        except Exception as exc:
            self._fail_preallocated(turn, self._safe_error(exc))
            close = getattr(runner, "close", None)
            if callable(close):
                close()
            return
        try:
            runtime = runner.new_runtime(task=self._turn_prompt(turn), session_id=node.session_id)
            runtime.state.permission_mode = turn.permission_mode
            runtime.state.running_mode = turn.running_mode
            runtime.services.runtime_store = _CanonicalRuntimeStore(self._store, node.session_id)
            bridge.bind_runtime(runtime)
            current = bridge.start()
            with self._state_lock:
                self._active_bridges[node.thread_id] = bridge
            runtime.run.thread_id = current.thread_id
            runtime.run.turn_id = current.id
            runtime.run.data_idx = current.current_data_idx
            self._apply_context_prefix(runtime, node.session_id, node.thread_id)
            runtime.services.on_event = bridge.handle
            from backend.storage.message_queue import RedisAgentMailbox

            mailbox = RedisAgentMailbox(
                self._queue,
                turn.id,
                node.thread_id,
                f"agent-{node.thread_id}-{uuid4().hex}",
            )
            runtime.services.steering = mailbox.take
            runtime.services.suspend_requested = lambda: self._status_requested(node.thread_id, "paused")
            runtime.services.complete_requested = lambda: self._status_requested(node.thread_id, "success")
            runtime.services.interrupt = self._approval_handler(creator_thread_id, node.thread_id)
            if initial_delivery_id:
                claim_method = "claim_thread_recovery" if recover_delivery else "claim_thread"
                claimed = getattr(self._queue, claim_method)(
                    node.thread_id,
                    f"agent-initial-{node.thread_id}-{uuid4().hex}",
                )
                if claimed is None or claimed.envelope.delivery_id != initial_delivery_id:
                    raise RuntimeError("The preallocated Agent delivery could not be claimed in FIFO order.")
                getattr(self._queue, "ack")(claimed)
            result = runner.run(runtime)
            if str(getattr(result, "status", "")) in {"completed", "success"}:
                bridge.finish("success", str(getattr(result, "final_answer", "") or ""))
            elif str(getattr(result, "stop_reason", "")) == "user_paused":
                bridge.finish("paused", str(getattr(result, "final_answer", "") or ""), category="user")
            else:
                bridge.finish(
                    "failed",
                    str(getattr(result, "final_answer", "") or "Agent execution failed."),
                    category="agent",
                )
        except Exception as exc:
            bridge.finish_exception(exc)
        finally:
            current_turn = getattr(self._store, "get_node")(node.session_id, turn.id)
            if isinstance(current_turn, RuntimeState) and current_turn.status in {"success", "failed"}:
                self._publish_turn_reports(node, current_turn)
            if (
                self._thread_events is not None
                and isinstance(current_turn, RuntimeState)
                and current_turn.status != "running"
            ):
                self._thread_events.finish_turn(node.thread_id, current_turn)
            with self._state_lock:
                if self._active_bridges.get(node.thread_id) is bridge:
                    self._active_bridges.pop(node.thread_id, None)
            if mailbox is not None:
                mailbox.close()
            close = getattr(runner, "close", None)
            if callable(close):
                close()
            current = getattr(self._store, "get_runtime_thread")(node.session_id, node.thread_id)
            current_node = (
                getattr(self._store, "get_node")(node.session_id, current.current_turn_id)
                if current is not None and current.current_turn_id
                else None
            )
            if (
                current is not None
                and current.running_turn_id is None
                and isinstance(current_node, RuntimeState)
                and current_node.status != "paused"
            ):
                try:
                    self._wake_thread(node.session_id, node.thread_id)
                except Exception:
                    pass

    def _apply_context_prefix(self, runtime: AgentRuntime, session_id: str, thread_id: str) -> None:
        context = getattr(self._store, "get_thread_context")(session_id, thread_id)
        if context is None:
            runtime.services.context_prefix_messages = []
            return
        if context.effective_strategy == "share" and context.snapshot:
            nodes = [runtime_node_from_dict(item) for item in context.snapshot]
            runtime.services.context_prefix_messages = _chat_messages_from_nodes(
                [item for item in nodes if isinstance(item, RuntimeState)]
            )
        elif context.effective_strategy == "compaction_share" and context.summary:
            runtime.services.context_prefix_messages = [
                SystemMessage(name="context_summary", content=f"{CHECKPOINT_PREAMBLE}\n\n{context.summary}")
            ]
        else:
            runtime.services.context_prefix_messages = []

    @staticmethod
    def _turn_prompt(turn: RuntimeState) -> str:
        return "".join(
            str(item.get("text") or "") for item in turn.user_message.get("content", []) if item.get("type") == "text"
        )

    def _approval_handler(self, creator_thread_id: str, child_thread_id: str):
        def approve(request: InterruptRequest):
            with self._state_lock:
                channel = self._approval_channels.get(creator_thread_id)
            if channel is None:
                raise ToolError("Subagent approval channel is unavailable; request denied fail-closed.")
            enriched = InterruptRequest(
                request.kind,
                request.message,
                {**request.data, "source_thread_id": child_thread_id},
                request.questions,
            )
            try:
                return channel(enriched)
            except Exception as exc:
                raise ToolError("Subagent approval channel is unavailable; request denied fail-closed.") from exc

        return approve

    def _fail_preallocated(self, turn: RuntimeState, message: str) -> None:
        current = getattr(self._store, "get_node")(turn.session_id, turn.id)
        if not isinstance(current, RuntimeState) or current.status != "running":
            return
        current.data[current.current_data_idx][-1]["content"].append(
            terminal_error_payload("agent", message, retryable=False)
        )
        current.status = "failed"
        current.timestamp = utc_iso()
        final = RuntimeState.from_dict(current.to_dict())
        getattr(self._store, "finalize_node")(final)
        node = getattr(self._store, "get_thread_node")(turn.session_id, turn.thread_id)
        if node is not None:
            self._publish_turn_reports(node, final)
        if self._thread_events is not None:
            self._thread_events.publish_frame(turn.thread_id, NodeFrame.snapshot(final), final)
            self._thread_events.finish_turn(turn.thread_id, final)

    def _status_requested(self, thread_id: str, status: str) -> bool:
        with self._state_lock:
            control = self._status_controls.get(thread_id)
            if control is None or control.requested_status != status:
                return False
            control.claimed = True
            return True

    def recover_session(self, session_id: str) -> None:
        if self._store is None or self._queue is None:
            return
        for runtime_thread in getattr(self._store, "list_runtime_threads")(session_id):
            node = getattr(self._store, "get_thread_node")(session_id, runtime_thread.thread_id)
            if node is None:
                continue
            if runtime_thread.running_turn_id:
                if runtime_thread.origin_kind != "subagent":
                    continue
                turn = getattr(self._store, "get_node")(session_id, runtime_thread.running_turn_id)
                if isinstance(turn, RuntimeState) and turn.status == "running":
                    turn = getattr(self._store, "settle_indeterminate_tool_calls")(turn.id)
                    delivery_id = str(turn.user_message.get("delivery_id") or "")
                    pending = getattr(self._queue, "peek_thread")(node.thread_id)
                    recover_delivery_id = (
                        delivery_id if pending is not None and pending.delivery_id == delivery_id else None
                    )
                    self._submit_turn(
                        node,
                        turn,
                        creator_thread_id=node.parent_thread_id or session_id,
                        initial_delivery_id=recover_delivery_id,
                        recover_delivery=recover_delivery_id is not None,
                    )
            else:
                self._wake_thread(session_id, runtime_thread.thread_id)
                self._drain_inactive_reports(session_id, runtime_thread.thread_id)
        self._dispatch_ready_reports(session_id)

    @staticmethod
    def _safe_error(error: BaseException) -> str:
        text, _data = persistent_event(RuntimeEvent("error", str(error)), True)
        text = text.strip() or error.__class__.__name__
        text = " ".join(text.split())
        return text if len(text) <= 2_000 else f"{text[:2_000]}…"
