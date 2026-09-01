"""Tool-facing Subagent actions: delegation, messaging, status, and queries."""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping
from pathlib import Path
from threading import Event
from typing import Any
from uuid import uuid4

from backend.domain import (
    MessageEnvelope,
    RuntimeThread,
    ThreadContext,
    ThreadNode,
)
from backend.domain.runtime_state import (
    NodeFrame,
    RuntimeState,
    new_node_id,
    new_thread_id,
    runtime_node_from_dict,
    utc_iso,
)
from backend.tools import ToolError

from ..core.context import AgentRuntime
from .contracts import _StatusControl


class _SubagentToolActionsMixin:
    """Implement the tools exposed by SubagentCoordinator."""

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
