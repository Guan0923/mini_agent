"""Persistent same-Session Agent Threads and Redis mailbox coordination."""

from __future__ import annotations

import copy
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from threading import BoundedSemaphore, Event, RLock, Thread, local
from time import monotonic
from typing import Any, Protocol
from uuid import uuid4

from backend.domain import (
    CHECKPOINT_PREAMBLE,
    AssistantMessage,
    MessageEnvelope,
    RuntimeThread,
    SystemMessage,
    ThreadContext,
    ThreadNode,
    TurnTrace,
)
from backend.domain.runtime_state import (
    NodeFrame,
    RuntimeState,
    new_node_id,
    new_thread_id,
    runtime_node_from_dict,
    terminal_error_payload,
    utc_iso,
)
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


class AgentThreadEvents(Protocol):
    def start_turn(self, turn: RuntimeState) -> None: ...

    def publish_frame(self, thread_id: str, frame: NodeFrame, current: RuntimeState) -> None: ...

    def finish_turn(self, thread_id: str, turn: RuntimeState) -> None: ...


@dataclass(frozen=True, slots=True)
class _SessionBinding:
    runner_factory: Callable[[], ChildRunner]
    workspace: Path
    project_workspace: Path | None = None


@dataclass(slots=True)
class _StatusControl:
    token: str
    requested_status: str
    settled: Event
    claimed: bool = False


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

    def get_node(self, _session_id: str, turn_id: str) -> object | None:
        return getattr(self.store, "get_node")(self.session_id, turn_id)

    def initialize_turn_trace(self, _session_id: str, trace: TurnTrace) -> TurnTrace:
        return getattr(self.store, "initialize_turn_trace")(self.session_id, trace)

    def register_turn_report(self, turn_id: str, agent_thread_id: str, recipient_thread_id: str) -> object:
        return getattr(self.store, "register_agent_turn_report")(
            self.session_id,
            turn_id,
            agent_thread_id,
            recipient_thread_id,
        )


class SubagentCoordinator:
    """Process-owned coordinator backed by SQLite and Redis rather than batch state."""

    _TOOLS = {
        "delegate_tasks",
        "send_agent_message",
        "set_thread_node_status",
        "get_thread_node",
        "pause_current_turn",
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
        thread_events: AgentThreadEvents | None = None,
    ) -> None:
        self._settings = settings or SubagentSettings()
        self._store = store
        self._queue = message_queue
        self._index = index
        self._job_registry = job_registry
        self._thread_events = thread_events
        self._bindings: dict[str, _SessionBinding] = {}
        self._jobs: dict[str, ThreadJob] = {}
        self._active_bridges: dict[str, RuntimeEventNodeBridge] = {}
        self._approval_channels: dict[str, Callable[[InterruptRequest], object]] = {}
        self._status_controls: dict[str, _StatusControl] = {}
        self._locks = WorkspaceWriteLock()
        self._state_lock = RLock()
        self._worker_slots = BoundedSemaphore(self._settings.max_workers)
        self._report_dispatch_stop = Event()
        self._report_dispatch_wakeup = Event()
        self._report_dispatcher: Thread | None = None
        self._report_retry: dict[str, tuple[int, float]] = {}
        self._reply_context = local()
        if child_runner_factory is not None:
            self._bindings["*"] = _SessionBinding(child_runner_factory, (workspace or Path(".")).resolve())

    def bind_session(
        self,
        session_id: str,
        runner_factory: Callable[[], ChildRunner],
        workspace: Path,
        project_workspace: Path | None = None,
    ) -> None:
        with self._state_lock:
            self._bindings[session_id] = _SessionBinding(
                runner_factory,
                workspace.resolve(),
                project_workspace.resolve() if project_workspace is not None else None,
            )
        self._ensure_report_dispatcher()
        self.recover_session(session_id)

    def close(self) -> None:
        self._report_dispatch_stop.set()
        self._report_dispatch_wakeup.set()
        dispatcher = self._report_dispatcher
        if dispatcher is not None and dispatcher.is_alive():
            dispatcher.join(timeout=2.0)

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
        if name == "get_thread_node":
            return self._get_nodes(runtime, arguments)
        if name == "pause_current_turn":
            return self._pause_current_turn(runtime)
        raise ToolError(f"Unknown subagent tool: {name}")

    def _pause_current_turn(self, runtime: AgentRuntime) -> str:
        source_id = self._actual_source(runtime, None, optional=True)
        turn = self._source_turn(runtime, source_id)
        if runtime.run.status != "running" or turn.status != "running":
            raise ToolError("pause_current_turn requires the caller's canonical Turn to be running.")
        if runtime.services.pause_after_tool:
            raise ToolError("The current Turn already has a pending pause request.")
        runtime.services.pause_after_tool = True
        return "thread_status: paused"

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
        path = self._thread_path(arguments.get("subagent_path"), "subagent_path")
        task = arguments.get("subagent_task")
        strategy = arguments.get("context_transfer_strategy")
        if not isinstance(task, str) or not task.strip() or len(task) > 20_000:
            raise ToolError("subagent_task must contain text.")
        if strategy not in {"share", "compaction_share", "independent"}:
            raise ToolError("context_transfer_strategy must use share, compaction_share, or independent.")
        if path == "/root":
            raise ToolError("The /root Agent already exists.")
        if (
            getattr(self._index, "thread_for_path")(runtime.state.session_id, source_node.root_thread_id, path)
            is not None
        ):
            raise ToolError(f"Agent Thread path already exists: {path}")
        parent_path = path.rsplit("/", 1)[0]
        parent_id = getattr(self._index, "thread_for_path")(
            runtime.state.session_id,
            source_node.root_thread_id,
            parent_path,
        )
        parent_node = (
            getattr(self._store, "get_thread_node")(runtime.state.session_id, parent_id) if parent_id else None
        )
        if parent_node is None or parent_node.root_thread_id != source_node.root_thread_id:
            raise ToolError(f"Agent Thread parent path does not exist in this root tree: {parent_path}")
        if parent_node.depth >= self._settings.max_depth:
            raise ToolError(f"subagents.max_depth={self._settings.max_depth} prevents deeper delegation.")

        source_turn = self._source_turn(runtime, source_id)
        frozen = self._freeze_snapshot(runtime, source_turn)
        summary: str | None = None
        effective = strategy
        if strategy == "compaction_share":
            try:
                summary = self._compact_snapshot(runtime, frozen)
                if not summary:
                    raise RuntimeError("Context compaction returned no summary.")
            except Exception:
                effective = "independent"

        timestamp = utc_iso()
        permission_mode = source_turn.permission_mode or runtime.state.permission_mode
        workspace = source_turn.cwd or runtime.state.workspace_root or ""
        project_workspace = source_turn.project_cwd or runtime.state.project_cwd or ""
        from backend.storage.sqlite_agent_threads import AgentThreadCreate

        thread_id = new_thread_id()
        turn_id = new_node_id()
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
            project_cwd=project_workspace,
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
            source_node.root_thread_id,
            parent_node.thread_id,
            path,
            "running",
            parent_node.depth + 1,
            timestamp,
            timestamp,
        )
        context = ThreadContext(
            thread_id,
            strategy,  # type: ignore[arg-type]
            effective,  # type: ignore[arg-type]
            source_turn.id,
            source_turn.current_data_idx,
            frozen if effective == "share" else None,
            summary if effective == "compaction_share" else None,
        )
        try:
            getattr(self._store, "create_agent_thread")(
                runtime.state.session_id,
                AgentThreadCreate(runtime_thread, node, context, turn, source_id),
            )
        except Exception as exc:
            raise ToolError(str(exc)) from exc

        channel = runtime.services.interrupt
        if channel is not None:
            with self._state_lock:
                self._approval_channels[source_id] = channel
        self._submit_turn(node, turn, creator_thread_id=source_id)
        return json.dumps(self._node_status(runtime.state.session_id, thread_id), ensure_ascii=False)

    @staticmethod
    def _thread_path(value: object, name: str) -> str:
        if not isinstance(value, str) or not value.startswith("/root") or len(value) > 1000:
            raise ToolError(f"{name} must be a complete /root Agent path.")
        segments = value.split("/")[1:]
        if (
            not segments
            or segments[0] != "root"
            or any(
                not segment or segment in {".", ".."} or "\\" in segment or segment != segment.strip()
                for segment in segments
            )
        ):
            raise ToolError(f"{name} contains an invalid path segment.")
        return "/" + "/".join(segments)

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
        source_id = self._actual_source(runtime, arguments.get("source_thread_id"), optional=True)
        target_path = self._thread_path(arguments.get("target_thread_path"), "target_thread_path")
        content = arguments.get("subagent_task")
        if not isinstance(content, str) or not content.strip() or len(content) > 20_000:
            raise ToolError("subagent_task must contain text.")
        session_id = runtime.state.session_id
        source = getattr(self._store, "get_thread_node")(session_id, source_id)
        if source is None:
            raise ToolError("The calling Thread is not an Agent-tree node.")
        target_id = getattr(self._index, "thread_for_path")(session_id, source.root_thread_id, target_path)
        target = getattr(self._store, "get_thread_node")(session_id, target_id) if target_id else None
        if target is None:
            raise ToolError("The target Agent path does not exist in the caller's root tree.")
        if source_id == target_id:
            raise ToolError("An Agent Thread cannot send a message to itself.")
        references = self._parse_references(session_id, target, arguments.get("references", []))
        need_reply = arguments.get("need_reply", False)
        if not isinstance(need_reply, bool):
            raise ToolError("need_reply must be boolean.")
        self._dispatch_message(
            session_id,
            source_id,
            target_id,
            content,
            references=references,
            need_reply=need_reply,
        )
        return json.dumps(self._node_status(session_id, target_id), ensure_ascii=False)

    def _parse_references(
        self,
        session_id: str,
        target: ThreadNode,
        value: object,
    ) -> list[dict[str, str]]:
        if not isinstance(value, list):
            raise ToolError("references must be an array.")
        if len(value) > 100:
            raise ToolError("references may contain at most 100 files.")
        if not value:
            return []
        runtime_thread = getattr(self._store, "get_runtime_thread")(session_id, target.thread_id)
        turn = (
            getattr(self._store, "get_node")(session_id, runtime_thread.current_turn_id)
            if runtime_thread is not None and runtime_thread.current_turn_id
            else None
        )
        if not isinstance(turn, RuntimeState) or not turn.cwd:
            raise ToolError("The target Agent has no canonical Session workspace.")
        roots = [Path(turn.cwd).resolve()]
        if turn.project_cwd:
            roots.append(Path(turn.project_cwd).resolve())
        references: list[dict[str, str]] = []
        for item in value:
            if not isinstance(item, Mapping) or set(item) != {"path"}:
                raise ToolError("Every reference must contain only path.")
            raw_path = item.get("path")
            path = Path(raw_path) if isinstance(raw_path, str) else Path()
            if not isinstance(raw_path, str) or len(raw_path) > 4000 or not path.is_absolute():
                raise ToolError("Every reference path must be absolute.")
            try:
                resolved = path.resolve(strict=True)
            except OSError as exc:
                raise ToolError(f"Referenced file does not exist: {raw_path}") from exc
            if not resolved.is_file():
                raise ToolError(f"Referenced path is not a file: {raw_path}")
            if not any(resolved.is_relative_to(root) for root in roots):
                raise ToolError(f"Referenced file is outside the Session and project workspaces: {raw_path}")
            references.append({"path": str(resolved)})
        return references

    def _dispatch_message(
        self,
        session_id: str,
        source_thread_id: str,
        target_thread_id: str,
        content: str,
        *,
        correlation_id: str | None = None,
        references: list[dict[str, str]] | None = None,
        runtime_config: Mapping[str, object] | None = None,
        need_reply: bool = False,
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
            payload={
                "content": content,
                "references": [dict(item) for item in references or []],
                "need_reply": need_reply,
                **({"runtime_config": dict(runtime_config)} if runtime_config else {}),
            },
            source_message_ids=(delivery_id,),
            correlation_id=correlation_id,
        )
        getattr(self._queue, "dispatch_agent")(envelope)
        wake = self._wake_thread(session_id, target_thread_id)
        return {"delivery_id": delivery_id, "accepted": True, **wake}

    def _set_status(self, runtime: AgentRuntime, arguments: dict[str, Any]) -> str:
        source_id = self._actual_source(runtime, arguments.get("source_thread_id"), optional=True)
        target_path = self._thread_path(arguments.get("target_thread_path"), "target_thread_path")
        status = arguments.get("thread_status")
        if status not in {"running", "paused", "success"}:
            raise ToolError("thread_status must be running, paused, or success.")
        session_id = runtime.state.session_id
        source = getattr(self._store, "get_thread_node")(session_id, source_id)
        if source is None:
            raise ToolError("The calling Thread is not an Agent-tree node.")
        target_id = getattr(self._index, "thread_for_path")(session_id, source.root_thread_id, target_path)
        target = getattr(self._store, "get_thread_node")(session_id, target_id) if target_id else None
        if target is None or target.parent_thread_id != source_id:
            raise ToolError("source_thread_id may manage only a direct child Agent Thread.")
        current = target.thread_status
        allowed = {"running": {"paused", "success"}, "paused": {"running", "success"}}
        if status not in allowed.get(current, set()):
            raise ToolError(f"Cannot change an Agent from {current} to {status}.")
        runtime_thread = getattr(self._store, "get_runtime_thread")(session_id, target.thread_id)
        turn_id = runtime_thread.current_turn_id if runtime_thread is not None else None
        turn = getattr(self._store, "get_node")(session_id, turn_id) if turn_id else None
        if not isinstance(turn, RuntimeState):
            raise ToolError("The target Agent has no canonical current Turn.")
        if current == "paused" and status == "success":
            completed = getattr(self._store, "complete_paused_turn")(turn.id)
            self._publish_turn_reports(target, completed)
            if self._thread_events is not None:
                self._thread_events.publish_frame(target.thread_id, NodeFrame.snapshot(completed), completed)
                self._thread_events.finish_turn(target.thread_id, completed)
        elif current == "paused" and status == "running":
            resumed = getattr(self._store, "resume_turn_node")(turn.id)
            self._submit_turn(target, resumed, creator_thread_id=source_id)
        else:
            control = _StatusControl(uuid4().hex, str(status), Event())
            with self._state_lock:
                if target.thread_id in self._status_controls:
                    raise ToolError("The target Agent already has a pending status change.")
                self._status_controls[target.thread_id] = control
            if not control.settled.wait(15.0):
                revoked = False
                with self._state_lock:
                    if self._status_controls.get(target.thread_id) is control and not control.claimed:
                        self._status_controls.pop(target.thread_id, None)
                        revoked = True
                actual = self._node_status(session_id, target.thread_id)["thread_status"]
                if not revoked:
                    raise ToolError(
                        f"Timed out after Agent accepted status {status} at a safe boundary; current status is {actual}."
                    )
                raise ToolError(
                    f"Timed out waiting for Agent status {status}; the request was revoked and current status is {actual}."
                )
            actual = self._node_status(session_id, target.thread_id)["thread_status"]
            if actual != status:
                raise ToolError(f"Agent ended as {actual} before it could change to {status}.")
        return json.dumps(self._node_status(session_id, target.thread_id), ensure_ascii=False)

    def _get_nodes(self, runtime: AgentRuntime, arguments: dict[str, Any]) -> str:
        source_id = self._actual_source(runtime, arguments.get("source_thread_id"), optional=True)
        session_id = runtime.state.session_id
        source = getattr(self._store, "get_thread_node")(session_id, source_id)
        if source is None:
            raise ToolError("The source Thread is not an Agent-tree node in this Session.")
        requested = arguments.get("target_thread_path")
        if requested is None or requested == "":
            nodes = getattr(self._store, "list_descendant_thread_nodes")(session_id, source_id)
        else:
            path = self._thread_path(requested, "target_thread_path")
            if path != source.thread_path and not path.startswith(f"{source.thread_path}/"):
                raise ToolError("target_thread_path must be the source Agent or one of its descendants.")
            target_id = getattr(self._index, "thread_for_path")(session_id, source.root_thread_id, path)
            target = getattr(self._store, "get_thread_node")(session_id, target_id) if target_id else None
            if target is None:
                raise ToolError("The target Agent path does not exist in the source subtree.")
            nodes = [target]
        return json.dumps([self._node_query_result(session_id, node) for node in nodes], ensure_ascii=False)

    def list_children(self, session_id: str, source_thread_id: str) -> list[dict[str, str]]:
        self._require_services()
        source = getattr(self._store, "get_thread_node")(session_id, source_thread_id)
        if source is None:
            raise ToolError("The source Agent Thread does not exist in this Session tree.")
        children = getattr(self._store, "list_child_thread_nodes")(session_id, source_thread_id)
        return [{"thread_id": node.thread_id, **self._node_query_result(session_id, node)} for node in children]

    def _node_status(self, session_id: str, thread_id: str) -> dict[str, str]:
        node = getattr(self._store, "get_thread_node")(session_id, thread_id)
        if node is None:
            raise ToolError("The Agent Thread no longer exists.")
        return {"thread_path": node.thread_path, "thread_status": node.thread_status}

    def _node_query_result(self, session_id: str, node: ThreadNode) -> dict[str, str]:
        runtime_thread = getattr(self._store, "get_runtime_thread")(session_id, node.thread_id)
        turn = (
            getattr(self._store, "get_node")(session_id, runtime_thread.current_turn_id)
            if runtime_thread is not None and runtime_thread.current_turn_id
            else None
        )
        result = ""
        if isinstance(turn, RuntimeState):
            if turn.status == "failed":
                error = next(
                    (
                        str(item.get("message") or "Execution failed.")
                        for item in reversed(turn.assistant_items)
                        if item.get("type") == "error"
                    ),
                    "Execution failed.",
                )
                result = (
                    f"{error}\n\nThis agent's turn failed. If you still need this agent, "
                    "use the send_agent_message tool to give it another task."
                )
            else:
                result = next(
                    (
                        str(item.get("text") or "")
                        for message in reversed(turn.data[turn.current_data_idx])
                        if message.get("role") == "assistant"
                        for item in reversed(message.get("content", []))
                        if item.get("type") == "text"
                    ),
                    "",
                )
        return {"thread_path": node.thread_path, "thread_status": node.thread_status, "task_result": result}

    @staticmethod
    def _turn_task_result(turn: RuntimeState) -> str:
        return next(
            (
                str(item.get("text") or "")
                for message in reversed(turn.data[turn.current_data_idx])
                if message.get("role") == "assistant"
                for item in reversed(message.get("content", []))
                if item.get("type") == "text"
            ),
            "",
        )

    @staticmethod
    def _turn_error(turn: RuntimeState) -> str:
        return next(
            (
                str(item.get("message") or "Execution failed.")
                for message in reversed(turn.data[turn.current_data_idx])
                if message.get("role") == "assistant"
                for item in reversed(message.get("content", []))
                if item.get("type") == "error"
            ),
            "Execution failed.",
        )

    def _reply_content(self, node: ThreadNode, turn: RuntimeState) -> str:
        if turn.status not in {"success", "failed"}:
            raise ValueError("Only a terminal Agent Turn can create a report.")
        if turn.status == "failed":
            result = (
                f"{self._turn_error(turn)}\n"
                "This agent's turn failed. If you still need this agent, "
                "use the send_agent_message tool to give it another task."
            )
        else:
            result = self._turn_task_result(turn)
        return f"thread_path: {node.thread_path}\nthread_status: {turn.status}\ntask_result: {result}"

    def _publish_turn_reports(self, node: ThreadNode, turn: RuntimeState) -> None:
        if turn.status not in {"success", "failed"}:
            return
        reply_content = self._reply_content(node, turn)

        self._reply_context.value = (node.session_id, turn.id, turn.status)
        try:
            self.reply_subagent_message(reply_content)
        finally:
            del self._reply_context.value
        self._dispatch_ready_reports(node.session_id)
        self._report_dispatch_wakeup.set()

    def reply_subagent_message(self, reply_content: str) -> None:
        """Finalize the pre-bound terminal Turn's reports without model-supplied routing."""

        context = getattr(self._reply_context, "value", None)
        if context is None:
            raise RuntimeError("reply_subagent_message has no bound terminal Agent Turn.")
        session_id, turn_id, thread_status = context
        getattr(self._store, "finalize_agent_turn_reports")(
            session_id,
            turn_id,
            thread_status=thread_status,
            reply_content=reply_content,
        )

    def _ensure_report_dispatcher(self) -> None:
        with self._state_lock:
            if self._report_dispatcher is not None and self._report_dispatcher.is_alive():
                return
            self._report_dispatch_stop.clear()
            self._report_dispatcher = Thread(
                target=self._report_dispatch_loop,
                name="agent-report-dispatcher",
                daemon=True,
            )
            self._report_dispatcher.start()

    def _report_dispatch_loop(self) -> None:
        while not self._report_dispatch_stop.is_set():
            with self._state_lock:
                sessions = tuple(key for key in self._bindings if key != "*")
            for session_id in sessions:
                try:
                    self._dispatch_ready_reports(session_id)
                except Exception:
                    continue
            self._report_dispatch_wakeup.wait(0.5)
            self._report_dispatch_wakeup.clear()

    def _dispatch_ready_reports(self, session_id: str) -> None:
        reports = getattr(self._store, "list_agent_turn_reports")(
            session_id,
            states=("waiting", "queued"),
        )
        now = monotonic()
        for report in reports:
            if report.thread_status not in {"success", "failed"}:
                continue
            attempts, next_at = self._report_retry.get(report.delivery_id, (0, 0.0))
            if next_at > now:
                continue
            envelope = MessageEnvelope(
                delivery_id=report.delivery_id,
                sender_kind="agent",
                source_thread_id=report.agent_thread_id,
                target_kind="report",
                target_id=report.recipient_thread_id,
                session_id=report.session_id,
                thread_id=report.recipient_thread_id,
                payload={
                    "content": report.reply_content,
                    "report_status": report.thread_status,
                },
                source_message_ids=(report.delivery_id,),
                created_at=report.created_at,
                correlation_id=report.turn_id,
            )
            try:
                getattr(self._queue, "dispatch_report")(envelope)
                getattr(self._store, "mark_agent_turn_report_state")(
                    session_id,
                    report.delivery_id,
                    "queued",
                )
                self._report_retry.pop(report.delivery_id, None)
                self._drain_inactive_reports(session_id, report.recipient_thread_id)
            except Exception:
                delays = (0.5, 1.0, 2.0, 5.0)
                delay = delays[min(attempts, len(delays) - 1)]
                self._report_retry[report.delivery_id] = (attempts + 1, monotonic() + delay)

    def _ack_report(self, session_id: str, claimed: object) -> None:
        envelope = getattr(claimed, "envelope")
        getattr(self._store, "mark_agent_turn_report_state")(session_id, envelope.delivery_id, "delivered")
        getattr(self._queue, "ack")(claimed)

    def _drain_inactive_reports(self, session_id: str, thread_id: str) -> None:
        runtime_thread = getattr(self._store, "get_runtime_thread")(session_id, thread_id)
        if runtime_thread is None or runtime_thread.running_turn_id:
            return
        turn_id = runtime_thread.current_turn_id
        turn = getattr(self._store, "get_node")(session_id, turn_id) if turn_id else None
        target = getattr(self._store, "get_thread_node")(session_id, thread_id)
        if not isinstance(turn, RuntimeState) or target is None:
            return
        was_paused = turn.status == "paused"
        if turn.status not in {"paused", "success", "failed"}:
            return
        first_sender = ""
        while True:
            claimed = getattr(self._queue, "claim_report")(
                thread_id,
                f"agent-report-inactive-{thread_id}-{uuid4().hex}",
                recover=True,
            )
            if claimed is None:
                break
            envelope = claimed.envelope
            first_sender = first_sender or envelope.source_thread_id
            if not getattr(self._store, "has_canonical_delivery")(session_id, envelope.delivery_id):
                turn = getattr(self._store, "append_agent_report")(
                    session_id,
                    thread_id,
                    delivery_id=envelope.delivery_id,
                    reply_content=envelope.content,
                )
                if self._thread_events is not None:
                    self._thread_events.publish_frame(thread_id, NodeFrame.snapshot(turn), turn)
            self._ack_report(session_id, claimed)
        if was_paused and first_sender:
            current = getattr(self._store, "get_node")(session_id, turn.id)
            if isinstance(current, RuntimeState) and current.status == "paused":
                resumed = getattr(self._store, "resume_turn_node")(current.id)
                self._submit_turn(target, resumed, creator_thread_id=first_sender)

    def consume_runtime_reports(self, runtime: AgentRuntime) -> int:
        """Drain all FIFO Assistant reports at a running Turn safe boundary."""

        if self._store is None or self._queue is None:
            return 0
        thread_id = str(runtime.run.thread_id or runtime.state.session_id)
        turn_id = str(runtime.run.turn_id or "")
        if runtime.run.status != "running" or not thread_id or not turn_id:
            return 0
        count = 0
        while True:
            claimed = getattr(self._queue, "claim_report")(
                thread_id,
                f"agent-report-running-{thread_id}-{uuid4().hex}",
            )
            if claimed is None:
                break
            envelope = claimed.envelope
            if not getattr(self._store, "has_canonical_delivery")(runtime.state.session_id, envelope.delivery_id):
                publish = runtime.services.publish or (lambda _event: None)
                publish(
                    RuntimeEvent(
                        "subagent_report",
                        envelope.content,
                        {
                            "reply_content": envelope.content,
                            "delivery_id": envelope.delivery_id,
                            "report_status": envelope.payload.get("report_status"),
                        },
                    )
                )
                runtime.state.messages.append(AssistantMessage(name="subagent_report", content=envelope.content))
                runtime.run.history = runtime.state.messages
                if not getattr(self._store, "has_canonical_delivery")(
                    runtime.state.session_id,
                    envelope.delivery_id,
                ):
                    getattr(self._store, "append_agent_report")(
                        runtime.state.session_id,
                        thread_id,
                        delivery_id=envelope.delivery_id,
                        reply_content=envelope.content,
                    )
            self._ack_report(runtime.state.session_id, claimed)
            count += 1
        if count:
            runtime.save()
        return count

    def send_from_root(
        self,
        session_id: str,
        target_thread_id: str,
        content: str,
        *,
        references: list[dict[str, str]] | None = None,
        runtime_config: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        self._require_services()
        target = getattr(self._store, "get_thread_node")(session_id, target_thread_id)
        source = (
            getattr(self._store, "get_thread_node")(session_id, target.root_thread_id) if target is not None else None
        )
        if (
            source is None
            or source.depth != 0
            or source.root_thread_id != source.thread_id
            or target is None
            or target.session_id != session_id
            or target.root_thread_id != source.thread_id
            or target.depth <= 0
        ):
            raise ToolError("The target must be a Subagent Thread in this Session tree.")
        parsed_references = self._references_from_web(session_id, target, references or [])
        if not content.strip():
            raise ToolError("Agent message requires task text.")
        return self._dispatch_message(
            session_id,
            source.thread_id,
            target_thread_id,
            content,
            references=parsed_references,
            runtime_config=runtime_config,
        )

    def _references_from_web(
        self,
        session_id: str,
        target: ThreadNode,
        values: list[dict[str, str]],
    ) -> list[dict[str, str]]:
        if not values:
            return []
        runtime_thread = getattr(self._store, "get_runtime_thread")(session_id, target.thread_id)
        turn = (
            getattr(self._store, "get_node")(session_id, runtime_thread.current_turn_id)
            if runtime_thread is not None and runtime_thread.current_turn_id
            else None
        )
        if not isinstance(turn, RuntimeState):
            raise ToolError("The target Agent has no canonical current Turn.")
        absolute: list[dict[str, str]] = []
        for value in values:
            source, raw_path = value.get("source"), value.get("path")
            if source not in {"project", "upload"} or not isinstance(raw_path, str):
                raise ToolError("Browser Agent references require source and path.")
            relative = Path(raw_path)
            if relative.is_absolute():
                raise ToolError("Browser Agent reference paths must be relative.")
            root = Path(turn.cwd) / "uploads" if source == "upload" else Path(turn.project_cwd or turn.cwd)
            absolute.append({"path": str((root / relative).resolve())})
        return self._parse_references(session_id, target, absolute)

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


__all__ = ["LockedToolExecutor", "SubagentCoordinator", "WorkspaceWriteLock"]
