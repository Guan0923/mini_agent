"""Persistent same-Session Agent Threads and Redis mailbox coordination."""

from __future__ import annotations

import copy
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from threading import BoundedSemaphore, RLock
from typing import Any, Protocol
from uuid import uuid4

from backend.domain import (
    CHECKPOINT_PREAMBLE,
    MessageEnvelope,
    RuntimeThread,
    SystemMessage,
    ThreadContext,
    ThreadNode,
)
from backend.domain.runtime_state import RuntimeState, new_node_id, new_thread_id, runtime_node_from_dict, utc_iso
from backend.jobs import AdmissionPolicy, JobLane, JobScopeKind, ThreadJob
from backend.tools import ToolError

from .capability_settings import SubagentSettings
from .core.context import AgentRuntime
from .core.context.exchange import _chat_messages_from_nodes
from .core.contracts import InterruptRequest
from .core.events import RuntimeEvent
from .node_bridge import RuntimeEventNodeBridge
from .persistence.recording import persistent_event
from .subagent_tools import LockedToolExecutor, WorkspaceWriteLock


class ChildRunner(Protocol):
    tools: object
    subagents: object | None

    def new_runtime(self, *, task: str, session_id: str | None = None, **kwargs: object) -> AgentRuntime: ...

    def run(self, runtime: AgentRuntime) -> object: ...


@dataclass(frozen=True, slots=True)
class _SessionBinding:
    runner_factory: Callable[[], ChildRunner]
    workspace: Path


class _CanonicalRuntimeStore:
    """Runner persistence adapter; the canonical node bridge owns every message."""

    def __init__(self, store: object, session_id: str) -> None:
        self.store = store
        self.session_id = session_id

    def save_runtime(self, _state: object) -> None:
        return None

    def append_turn_input(
        self,
        _session_id: str,
        _run_id: str,
        _content: str,
        *,
        delivery_id: str | None = None,
    ) -> None:
        del delivery_id

    def has_turn_delivery(self, _session_id: str, delivery_id: str) -> bool:
        return bool(getattr(self.store, "has_canonical_delivery")(self.session_id, delivery_id))


class SubagentCoordinator:
    """Process-owned coordinator backed by SQLite and Redis rather than batch state."""

    _TOOLS = {
        "delegate_tasks",
        "send_agent_message",
        "set_thread_node_status",
        "list_current_node_sub_thread",
    }

    def __init__(
        self,
        child_runner_factory: Callable[[], ChildRunner] | None = None,
        workspace: Path | None = None,
        settings: SubagentSettings | None = None,
        *,
        store: object | None = None,
        message_queue: object | None = None,
        index: object | None = None,
        job_registry: object | None = None,
    ) -> None:
        self._settings = settings or SubagentSettings()
        self._store = store
        self._queue = message_queue
        self._index = index
        self._job_registry = job_registry
        self._bindings: dict[str, _SessionBinding] = {}
        self._jobs: dict[str, ThreadJob] = {}
        self._approval_channels: dict[str, Callable[[InterruptRequest], object]] = {}
        self._locks = WorkspaceWriteLock()
        self._state_lock = RLock()
        self._worker_slots = BoundedSemaphore(self._settings.max_workers)
        if child_runner_factory is not None:
            self._bindings["*"] = _SessionBinding(child_runner_factory, (workspace or Path(".")).resolve())

    def bind_session(
        self,
        session_id: str,
        runner_factory: Callable[[], ChildRunner],
        workspace: Path,
    ) -> None:
        with self._state_lock:
            self._bindings[session_id] = _SessionBinding(runner_factory, workspace.resolve())
        self.recover_session(session_id)

    @classmethod
    def handles(cls, name: str) -> bool:
        return name in cls._TOOLS

    def invoke(self, runtime: AgentRuntime, name: str, arguments: dict[str, Any]) -> str:
        self._require_services()
        if name == "delegate_tasks":
            return self._delegate(runtime, arguments)
        if name == "send_agent_message":
            return self._send(runtime, arguments)
        if name == "set_thread_node_status":
            return self._set_status(runtime, arguments)
        if name == "list_current_node_sub_thread":
            return self._list_children(runtime, arguments)
        raise ToolError(f"Unknown subagent tool: {name}")

    def _require_services(self) -> None:
        if self._store is None or self._queue is None or self._index is None or self._job_registry is None:
            raise ToolError("Persistent Subagents require WebAppState SQLite, Redis, index, and job services.")

    def _actual_source(self, runtime: AgentRuntime, requested: object, *, optional: bool) -> str:
        actual = str(runtime.run.thread_id or runtime.state.session_id)
        if optional and (requested is None or requested == ""):
            return actual
        if not isinstance(requested, str) or not requested:
            raise ToolError("source_thread_id must be a non-empty string.")
        if requested != actual:
            raise ToolError("source_thread_id does not match the actual calling Thread.")
        return actual

    def _source_turn(self, runtime: AgentRuntime, source_thread_id: str) -> RuntimeState:
        turn_id = str(runtime.run.turn_id or getattr(self._index, "head_for_thread")(source_thread_id) or "")
        node = getattr(self._store, "get_node")(runtime.state.session_id, turn_id) if turn_id else None
        if not isinstance(node, RuntimeState) or node.thread_id != source_thread_id:
            raise ToolError("The calling Thread has no canonical current Turn.")
        return node

    def _delegate(self, runtime: AgentRuntime, arguments: dict[str, Any]) -> str:
        source_id = self._actual_source(runtime, arguments.get("source_thread_id"), optional=True)
        source_node = getattr(self._store, "get_thread_node")(runtime.state.session_id, source_id)
        if source_node is None:
            raise ToolError("The calling Thread is not an Agent-tree node.")
        count, names, tasks, strategies = self._parse_delegate(arguments)
        if source_node.depth >= self._settings.max_depth:
            raise ToolError(f"subagents.max_depth={self._settings.max_depth} prevents deeper delegation.")
        paths = [f"{source_node.thread_path}/{name}" for name in names]
        if len(set(names)) != count or len(set(paths)) != count:
            raise ToolError("subagent_name values must be unique under the same parent.")
        for name, path in zip(names, paths, strict=True):
            if "/" in name or name in {".", ".."} or name != name.strip():
                raise ToolError("subagent_name cannot contain '/' or surrounding whitespace.")
            if getattr(self._index, "thread_for_path")(runtime.state.session_id, path) is not None:
                raise ToolError(f"Agent Thread path already exists: {path}")

        source_turn = self._source_turn(runtime, source_id)
        frozen = self._freeze_snapshot(runtime, source_turn)
        summary: str | None = None
        compaction_error = ""
        if "compaction_share" in strategies:
            try:
                summary = self._compact_snapshot(runtime, frozen)
                if not summary:
                    raise RuntimeError("Context compaction returned no summary.")
            except Exception as exc:
                compaction_error = self._safe_error(exc)

        timestamp = utc_iso()
        permission_mode = source_turn.permission_mode or runtime.state.permission_mode
        workspace = source_turn.cwd or runtime.state.workspace_root or ""
        batch = []
        created = []
        from backend.storage.sqlite_agent_threads import AgentThreadCreate

        for name, task, requested, path in zip(names, tasks, strategies, paths, strict=True):
            thread_id = new_thread_id()
            turn_id = new_node_id()
            effective = "independent" if requested == "compaction_share" and summary is None else requested
            turn = RuntimeState.create(
                session_id=runtime.state.session_id,
                thread_id=thread_id,
                id=turn_id,
                parent=source_turn,
                user_content=[{"type": "text", "text": task, "status": "success"}],
                provider_name=source_turn.provider_name,
                model=source_turn.model,
                permission_mode=permission_mode,
                running_mode="agent",
                cwd=workspace,
            )
            runtime_thread = RuntimeThread(
                runtime.state.session_id,
                thread_id,
                "subagent",
                turn_id,
                turn_id,
                timestamp,
                timestamp,
            )
            node = ThreadNode(
                runtime.state.session_id,
                thread_id,
                source_id,
                path,
                task,
                "opening",
                source_node.depth + 1,
                timestamp,
                timestamp,
            )
            context = ThreadContext(
                thread_id,
                requested,
                effective,  # type: ignore[arg-type]
                source_turn.id,
                source_turn.current_data_idx,
                frozen if effective == "share" else None,
                summary if effective == "compaction_share" else None,
            )
            created.append(AgentThreadCreate(runtime_thread, node, context, turn))
            batch.append((name, node, turn, context))
        try:
            getattr(self._store, "create_agent_threads")(runtime.state.session_id, created)
        except Exception as exc:
            raise ToolError(str(exc)) from exc

        channel = runtime.services.interrupt
        if channel is not None:
            with self._state_lock:
                self._approval_channels[source_id] = channel
        results: list[dict[str, object]] = []
        for name, node, turn, context in batch:
            admission = self._submit_turn(node, turn, creator_thread_id=source_id, auto_reply=True)
            results.append(
                {
                    "name": name,
                    "path": node.thread_path,
                    "session_id": node.session_id,
                    "thread_id": node.thread_id,
                    "turn_id": turn.id,
                    "status": node.thread_status,
                    "requested_strategy": context.requested_strategy,
                    "effective_strategy": context.effective_strategy,
                    "background_admission": admission,
                    **(
                        {"strategy_notice": f"compaction_share downgraded to independent: {compaction_error}"}
                        if context.requested_strategy != context.effective_strategy
                        else {}
                    ),
                }
            )
        return json.dumps({"subagent_count": count, "subagents": results}, ensure_ascii=False)

    def _parse_delegate(self, arguments: Mapping[str, Any]) -> tuple[int, list[str], list[str], list[str]]:
        count = arguments.get("subagent_count")
        names = arguments.get("subagent_name")
        tasks = arguments.get("subagent_tasks")
        strategies = arguments.get("context_transfer_strategy")
        if (
            isinstance(count, bool)
            or not isinstance(count, int)
            or not 1 <= count <= self._settings.max_tasks_per_batch
        ):
            raise ToolError(f"subagent_count must be between 1 and {self._settings.max_tasks_per_batch}.")
        if not all(isinstance(value, list) for value in (names, tasks, strategies)):
            raise ToolError("subagent_name, subagent_tasks, and context_transfer_strategy must be arrays.")
        if not (len(names) == len(tasks) == len(strategies) == count):
            raise ToolError("subagent_count and all three array lengths must match.")
        if any(not isinstance(value, str) or not value or len(value) > 128 for value in names):
            raise ToolError("Every subagent_name must be a non-empty string.")
        if any(not isinstance(value, str) or not value.strip() or len(value) > 20_000 for value in tasks):
            raise ToolError("Every subagent task must contain text.")
        allowed = {"share", "compaction_share", "independent"}
        if any(value not in allowed for value in strategies):
            raise ToolError("context_transfer_strategy must use share, compaction_share, or independent.")
        return count, list(names), list(tasks), list(strategies)

    @staticmethod
    def _freeze_snapshot(runtime: AgentRuntime, source: RuntimeState) -> list[dict[str, Any]]:
        nodes = runtime.model_nodes() or [source]
        frozen: list[dict[str, Any]] = []
        for candidate in nodes:
            node = RuntimeState.from_dict(candidate.to_dict())
            selected = node.data[node.current_data_idx]
            for message in selected:
                message["content"] = [item for item in message["content"] if item.get("status") in {None, "success"}]
            node = RuntimeState.from_dict(node.to_dict())
            frozen.append(node.to_dict())
        return frozen

    @staticmethod
    def _compact_snapshot(runtime: AgentRuntime, snapshot: list[dict[str, Any]]) -> str:
        planner = runtime.services.planner
        compact = getattr(planner, "compact_context", None)
        if not callable(compact):
            raise RuntimeError("The current planner does not support context compaction.")
        cloned_state = runtime.state.from_dict(runtime.state.to_dict())
        cloned_services = copy.copy(runtime.services)
        nodes = [runtime_node_from_dict(value) for value in snapshot]
        cloned_services.runtime_node_context = lambda: [
            node.clone() for node in nodes if isinstance(node, RuntimeState)
        ]
        cloned_services.runtime_store = None
        cloned_services.checkpoint_store = None
        cloned_services.publish = lambda _event: None
        cloned = AgentRuntime(cloned_state, cloned_services)
        result = compact(cloned)
        return str(getattr(result, "summary", "") or "").strip()

    def _send(self, runtime: AgentRuntime, arguments: dict[str, Any]) -> str:
        source_id = self._actual_source(runtime, arguments.get("source_thread_id"), optional=False)
        target_id = arguments.get("target_thread_id")
        content = arguments.get("subagent_tasks")
        if not isinstance(target_id, str) or not target_id:
            raise ToolError("target_thread_id must be a non-empty string.")
        if not isinstance(content, str) or not content.strip():
            raise ToolError("subagent_tasks must contain text.")
        if source_id == target_id:
            raise ToolError("An Agent Thread cannot send a message to itself.")
        session_id = runtime.state.session_id
        source = getattr(self._store, "get_thread_node")(session_id, source_id)
        target = getattr(self._store, "get_thread_node")(session_id, target_id)
        if source is None or target is None:
            raise ToolError("Both source and target must be nodes in the same Agent tree.")
        if target.thread_status != "opening":
            raise ToolError("Target Agent Thread is closed.")
        return json.dumps(self._dispatch_message(session_id, source_id, target_id, content), ensure_ascii=False)

    def _dispatch_message(
        self,
        session_id: str,
        source_thread_id: str,
        target_thread_id: str,
        content: str,
        *,
        correlation_id: str | None = None,
    ) -> dict[str, object]:
        delivery_id = f"agent_delivery_{uuid4().hex}"
        envelope = MessageEnvelope(
            delivery_id=delivery_id,
            sender_kind="agent",
            source_thread_id=source_thread_id,
            target_kind="thread",
            target_id=target_thread_id,
            session_id=session_id,
            thread_id=target_thread_id,
            payload={"content": content, "references": []},
            source_message_ids=(delivery_id,),
            correlation_id=correlation_id,
        )
        getattr(self._queue, "dispatch_agent")(envelope)
        wake = self._wake_thread(session_id, target_thread_id)
        return {"delivery_id": delivery_id, "accepted": True, **wake}

    def _set_status(self, runtime: AgentRuntime, arguments: dict[str, Any]) -> str:
        source_id = self._actual_source(runtime, arguments.get("source_thread_id"), optional=True)
        target_id = arguments.get("target_thread_id")
        status = arguments.get("thread_status")
        if not isinstance(target_id, str) or not target_id:
            raise ToolError("target_thread_id must be a non-empty string.")
        if status not in {"opening", "closed"}:
            raise ToolError("thread_status must be opening or closed.")
        target = getattr(self._store, "get_thread_node")(runtime.state.session_id, target_id)
        if target is None or target.parent_thread_id != source_id:
            raise ToolError("source_thread_id may manage only a direct child Agent Thread.")
        updated = getattr(self._store, "update_thread_status")(runtime.state.session_id, target_id, status)
        wake: dict[str, object] = {}
        if status == "opening":
            wake = self._wake_thread(runtime.state.session_id, target_id)
        return json.dumps({**updated.to_dict(), **wake}, ensure_ascii=False)

    def _list_children(self, runtime: AgentRuntime, arguments: dict[str, Any]) -> str:
        source_id = self._actual_source(runtime, arguments.get("source_thread_id"), optional=True)
        source = getattr(self._store, "get_thread_node")(runtime.state.session_id, source_id)
        if source is None:
            raise ToolError("The calling Thread is not an Agent-tree node.")
        children = getattr(self._store, "list_child_thread_nodes")(runtime.state.session_id, source_id)
        return json.dumps(
            [
                {
                    "thread_id": node.thread_id,
                    "thread_path": node.thread_path,
                    "thread_task": node.thread_task,
                    "thread_status": node.thread_status,
                }
                for node in children
            ],
            ensure_ascii=False,
        )

    def _wake_thread(self, session_id: str, thread_id: str) -> dict[str, object]:
        target = getattr(self._store, "get_thread_node")(session_id, thread_id)
        runtime_thread = getattr(self._store, "get_runtime_thread")(session_id, thread_id)
        if target is None or runtime_thread is None or target.thread_status != "opening":
            return {"target_state": "closed"}
        if runtime_thread.running_turn_id:
            return {"target_state": "running", "turn_id": runtime_thread.running_turn_id}
        envelope = getattr(self._queue, "peek_thread")(thread_id)
        if envelope is None:
            return {"target_state": "idle"}
        head_id = runtime_thread.current_turn_id
        parent = getattr(self._store, "get_node")(session_id, head_id) if head_id else None
        if not isinstance(parent, RuntimeState):
            return {"target_state": "idle", "background_admission": "missing_parent"}
        turn_id = new_node_id()
        item = {"type": "text", "text": envelope.content, "status": "success"}
        node = RuntimeState.create(
            session_id=session_id,
            thread_id=thread_id,
            id=turn_id,
            parent=parent,
            user_content=[item],
            provider_name=parent.provider_name,
            model=parent.model,
            permission_mode=parent.permission_mode,
            running_mode="agent",
            cwd=parent.cwd,
        )
        node.data[0][0]["delivery_id"] = envelope.delivery_id
        node = RuntimeState.from_dict(node.to_dict())
        try:
            getattr(self._store, "create_thread_turn_if_idle")(node, expected_head_id=head_id)
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
            auto_reply=False,
            initial_delivery_id=envelope.delivery_id,
        )
        return {"target_state": "started", "turn_id": turn_id, "background_admission": admission}

    def _submit_turn(
        self,
        node: ThreadNode,
        turn: RuntimeState,
        *,
        creator_thread_id: str,
        auto_reply: bool,
        initial_delivery_id: str | None = None,
        recover_delivery: bool = False,
    ) -> str:
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
                        auto_reply=auto_reply,
                        initial_delivery_id=initial_delivery_id,
                        recover_delivery=recover_delivery,
                    )
                finally:
                    with self._state_lock:
                        self._jobs.pop(node.thread_id, None)

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
        auto_reply: bool,
        initial_delivery_id: str | None,
        recover_delivery: bool,
    ) -> None:
        runner = binding.runner_factory()
        runner.subagents = self
        runner.tools = LockedToolExecutor(runner.tools, self._locks, binding.workspace)
        mailbox = None
        bridge = RuntimeEventNodeBridge(
            self._store,
            session_id=node.session_id,
            thread_id=node.thread_id,
            source_node_id=turn.id,
            adopt_existing=True,
            prompt="",
            permission_mode=turn.permission_mode,
            running_mode="agent",
            cwd=turn.cwd,
            isolated_thread_context=True,
            emit=lambda _frame: None,
        )
        try:
            runtime = runner.new_runtime(task=self._turn_prompt(turn), session_id=node.session_id)
            runtime.state.permission_mode = turn.permission_mode
            runtime.state.running_mode = "agent"
            runtime.services.runtime_store = _CanonicalRuntimeStore(self._store, node.session_id)
            bridge.bind_runtime(runtime)
            current = bridge.start()
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
            runtime.services.suspend_requested = lambda: self._is_closed(node.session_id, node.thread_id)
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
                final = bridge.finish("success", str(getattr(result, "final_answer", "") or ""))
            elif str(getattr(result, "stop_reason", "")) == "user_paused":
                final = bridge.finish("paused", str(getattr(result, "final_answer", "") or ""), category="user")
            else:
                final = bridge.finish(
                    "failed",
                    str(getattr(result, "final_answer", "") or "Agent execution failed."),
                    category="agent",
                )
            if auto_reply and final is not None:
                self._send_initial_result(node, final, creator_thread_id, result)
        except Exception as exc:
            final = bridge.finish_exception(exc)
            if auto_reply and final is not None:
                self._send_initial_result(node, final, creator_thread_id, None, error=self._safe_error(exc))
        finally:
            if mailbox is not None:
                mailbox.close()
            close = getattr(runner, "close", None)
            if callable(close):
                close()
            current = getattr(self._store, "get_runtime_thread")(node.session_id, node.thread_id)
            if current is not None and current.running_turn_id is None:
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

    def _send_initial_result(
        self,
        node: ThreadNode,
        final: RuntimeState,
        creator_thread_id: str,
        result: object | None,
        *,
        error: str = "",
    ) -> None:
        answer = str(getattr(result, "final_answer", "") or "") if result is not None else ""
        payload = json.dumps(
            {
                "type": "subagent_initial_result",
                "session_id": node.session_id,
                "thread_id": node.thread_id,
                "thread_path": node.thread_path,
                "turn_id": final.id,
                "status": final.status,
                "answer": answer,
                "error": error,
            },
            ensure_ascii=False,
        )
        self._dispatch_message(
            node.session_id,
            node.thread_id,
            creator_thread_id,
            payload,
            correlation_id=final.id,
        )

    def _fail_preallocated(self, turn: RuntimeState, message: str) -> None:
        current = getattr(self._store, "get_node")(turn.session_id, turn.id)
        if not isinstance(current, RuntimeState) or current.status != "running":
            return
        current.data[current.current_data_idx][-1]["content"].append(
            {"type": "error", "message": message, "status": "failed", "retryable": False}
        )
        current.status = "failed"
        current.timestamp = utc_iso()
        getattr(self._store, "finalize_node")(RuntimeState.from_dict(current.to_dict()))

    def _is_closed(self, session_id: str, thread_id: str) -> bool:
        node = getattr(self._store, "get_thread_node")(session_id, thread_id)
        return node is None or node.thread_status == "closed"

    def recover_session(self, session_id: str) -> None:
        if self._store is None or self._queue is None:
            return
        for runtime_thread in getattr(self._store, "list_runtime_threads")(session_id):
            node = getattr(self._store, "get_thread_node")(session_id, runtime_thread.thread_id)
            if node is None or node.thread_status != "opening":
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
                        auto_reply=turn.parent_thread_id != turn.thread_id,
                        initial_delivery_id=recover_delivery_id,
                        recover_delivery=recover_delivery_id is not None,
                    )
            else:
                self._wake_thread(session_id, runtime_thread.thread_id)

    @staticmethod
    def _safe_error(error: BaseException) -> str:
        text, _data = persistent_event(RuntimeEvent("error", str(error)), True)
        text = text.strip() or error.__class__.__name__
        text = " ".join(text.split())
        return text if len(text) <= 2_000 else f"{text[:2_000]}…"


__all__ = ["LockedToolExecutor", "SubagentCoordinator", "WorkspaceWriteLock"]
