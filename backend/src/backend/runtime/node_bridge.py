"""Bridge the legacy execution callbacks into RuntimeState node frames.

This is a migration seam: the runner may still report internal callbacks while
clients only receive the canonical create/update/delete protocol.  No legacy
event object is persisted or sent over SSE.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Mapping
from typing import Any

from backend.domain.runtime_state import (
    DEFAULT_COMPACTION_RETENTION,
    NodeFrame,
    NodeStatus,
    NodeWriter,
    RuntimeNodeStore,
    RuntimeState,
    TerminalErrorCategory,
    change_payload,
    compaction_payload,
    message_payload,
    terminal_error_payload,
    terminal_error_text,
)

_NETWORK_ERROR_TYPES = frozenset(
    {
        "ConnectionError",
        "ConnectTimeout",
        "ModelTransportError",
        "NetworkError",
        "ReadTimeout",
        "Timeout",
        "TimeoutError",
    }
)
_TOOL_ERROR_TYPES = frozenset({"ToolError", "ConfirmationRequired", "TaskPreparationError"})
_AGENT_ERROR_TYPES = frozenset(
    {
        "PlanningError",
        "ModelOutputError",
        "ProviderOutputError",
        "ModelRequestError",
        "ModelConfigurationError",
        "HookError",
    }
)
_HIDDEN_RECOVERABLE_EVENTS = frozenset(
    {"tool_failed", "tool_recovery", "model_repair", "model_retry", "replan_requested"}
)


class RuntimeEventNodeBridge:
    """Project execution events into durable canonical message nodes."""

    def __init__(
        self,
        store: RuntimeNodeStore,
        *,
        session_id: str,
        prompt: str,
        source_node_id: str | None = None,
        user: str = "",
        provider: str = "unknown",
        model: str = "unknown",
        cwd: str = "",
        thinking_level: str = "medium",
        emit: Callable[[NodeFrame], None],
    ) -> None:
        self.store = store
        self.session_id = session_id
        self.prompt = prompt
        self.source_node_id = source_node_id
        self.user = user
        self.provider = provider or "unknown"
        self.model = model or "unknown"
        self.cwd = cwd
        self.thinking_level = thinking_level or "medium"
        self.writer = NodeWriter(store, emit=emit)
        self.parent: RuntimeState | None = None
        self.assistant: RuntimeState | None = None
        self.last_node: RuntimeState | None = None
        self.assistant_blocks: list[dict[str, Any]] = []
        self.response_text = ""
        self.run_id = ""
        self.abort_category: TerminalErrorCategory | None = None
        self.abort_code = ""
        self.terminal_error: dict[str, str] | None = None
        self.started = False
        self.closed = False

    def start(self) -> RuntimeState:
        if self.started:
            if self.last_node is None:
                raise RuntimeError("Node bridge has no starting node.")
            return self.last_node
        if self.source_node_id:
            self.parent = self.store.get_node(self.session_id, self.source_node_id)
            if self.parent is None:
                raise ValueError("source_node_id does not belong to the active session.")
            if self.store.list_children(self.parent.session_id, self.parent.id):
                raise ValueError("source_node_id must identify a leaf node.")
            if not self.prompt and self.parent.status not in {"failed", "abort"}:
                raise ValueError("A resume source must be failed or abort.")
        elif self.parent is None:
            load_nodes = getattr(self.store, "load_nodes", None)
            if callable(load_nodes):
                existing = list(load_nodes(self.session_id))
                if existing:
                    parent_keys = {(item.parent_session_id, item.parent_id) for item in existing if item.parent_id}
                    leaves = [item for item in existing if (item.session_id, item.id) not in parent_keys]
                    if leaves:
                        self.parent = max(leaves, key=lambda item: (item.timestamp, item.id))
        if self.parent is not None and self.store.list_children(self.parent.session_id, self.parent.id):
            raise ValueError("The continuation parent must be a leaf node.")
        if self.parent is None and self.source_node_id is None:
            # A new session records configuration changes before its first
            # user message.  Existing sessions keep their current pointers.
            model_node = self.writer.create(
                session_id=self.session_id,
                data=change_payload("model_change", model=self.model, provider=self.provider),
                user=self.user,
                provider=self.provider,
                cwd=self.cwd,
            )
            self.writer.delete(model_node.session_id, model_node.id)
            thinking_node = self.writer.create(
                session_id=self.session_id,
                parent=model_node,
                data=change_payload("thinking_level_change", level=self.thinking_level),
            )
            self.writer.delete(thinking_node.session_id, thinking_node.id)
            self.parent = thinking_node
        if self.parent is not None and not self.prompt:
            # ``/resume`` has no new user text.  Continue directly from the
            # paused/failed leaf instead of persisting an empty user message.
            self.last_node = self.parent
            self.started = True
            return self.last_node
        user_node = self.writer.create(
            session_id=self.session_id,
            parent=self.parent,
            data=message_payload("user", self.prompt),
            user=self.user,
            provider=self.provider,
            cwd=self.cwd,
        )
        self.last_node = self.writer.delete(user_node.session_id, user_node.id)
        self.started = True
        return self.last_node

    def _ensure_assistant(self) -> RuntimeState:
        if self.assistant is None:
            self.assistant = self.writer.create(
                session_id=self.session_id,
                parent=self.last_node,
                data=message_payload("assistant", [], **({"run_id": self.run_id} if self.run_id else {})),
                user=self.user,
                provider=self.provider,
                cwd=self.cwd,
            )
            self.assistant_blocks = []
            self.response_text = ""
        return self.assistant

    def _update_assistant(self) -> None:
        if self.assistant is not None:
            metadata: dict[str, Any] = {}
            if self.run_id:
                metadata["run_id"] = self.run_id
            if self.terminal_error is not None:
                metadata["error"] = self.terminal_error
            self.writer.update_data(
                self.assistant,
                message_payload("assistant", self.assistant_blocks, **metadata),
            )

    def _remember_abort(
        self,
        category: TerminalErrorCategory,
        *,
        code: str = "",
    ) -> None:
        """Retain the strongest structured cause until the run terminates."""

        self.abort_category = category
        self.abort_code = code

    @staticmethod
    def _model_error_category(data: Mapping[str, Any]) -> TerminalErrorCategory:
        error_type = str(data.get("error_type") or "")
        if error_type in _NETWORK_ERROR_TYPES or any(
            token in error_type.lower() for token in ("connection", "network", "timeout", "transport")
        ):
            return "network"
        if error_type in _TOOL_ERROR_TYPES or "tool" in error_type.lower():
            return "tool"
        return "agent"

    @classmethod
    def _exception_category(cls, error: BaseException) -> TerminalErrorCategory | None:
        """Map a known runtime exception to an abort category.

        Unknown exceptions intentionally remain ``failed`` so that the
        generic failed message does not claim a cause the runtime cannot
        prove.  Provider transport errors and tool/preparation failures are
        safe to classify because their exception types are part of the local
        runtime contract.
        """

        error_type = error.__class__.__name__
        if error_type in _NETWORK_ERROR_TYPES or any(
            token in error_type.lower() for token in ("connection", "network", "timeout", "transport")
        ):
            return "network"
        if error_type in _TOOL_ERROR_TYPES or "tool" in error_type.lower():
            return "tool"
        if error_type in _AGENT_ERROR_TYPES:
            return "agent"
        return None

    @classmethod
    def _data_category(cls, data: Mapping[str, Any]) -> TerminalErrorCategory | None:
        error_type = str(data.get("error_type") or "")
        if not error_type:
            return None
        if error_type in _NETWORK_ERROR_TYPES or any(
            token in error_type.lower() for token in ("connection", "network", "timeout", "transport")
        ):
            return "network"
        if error_type in _TOOL_ERROR_TYPES or "tool" in error_type.lower():
            return "tool"
        if error_type in _AGENT_ERROR_TYPES:
            return "agent"
        return None

    @staticmethod
    def _json_value(value: Any) -> Any:
        """Detach an event payload without allowing runtime-only objects into nodes.

        RuntimeEvent is still an internal migration callback and a few older
        producers pass dataclass instances in ``data``.  The node protocol is
        deliberately JSON-only, so preserve ordinary values and render the
        exceptional ones as bounded strings instead of failing a whole run
        while trying to publish its diagnostic state.
        """

        if value is None or isinstance(value, (str, bool, int)):
            return value
        if isinstance(value, float):
            return value if math.isfinite(value) else str(value)
        if isinstance(value, Mapping):
            return {str(key): RuntimeEventNodeBridge._json_value(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [RuntimeEventNodeBridge._json_value(item) for item in value]
        try:
            json.dumps(value, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError):
            return str(value)
        return value

    def _append_event_block(
        self,
        block_type: str,
        kind: str,
        message: str,
        data: Mapping[str, Any],
    ) -> None:
        """Append one presentation-neutral control block to the dynamic node."""

        self._ensure_assistant()
        block = {str(key): self._json_value(value) for key, value in data.items()}
        # ``type`` is the discriminant; an old event payload must not be able
        # to overwrite it and make the message invalid.
        block["type"] = block_type
        block["event"] = kind
        if message and not isinstance(block.get("text"), str):
            block["text"] = message
        self.assistant_blocks.append(block)
        self._update_assistant()

    def _seal_assistant(self, status: NodeStatus = "success") -> None:
        if self.assistant is not None:
            self.last_node = self.writer.delete(self.session_id, self.assistant.id, status=status)
            self.assistant = None

    def _ancestor_path(self, source: RuntimeState | None) -> list[RuntimeState]:
        """Load the current cross-session path when the store exposes it."""

        if source is None:
            return []
        loader = getattr(self.store, "load_nodes", None)
        if not callable(loader):
            return [source]
        by_key = {node.key: node for node in loader(source.session_id)}
        current = by_key.get(source.key, source)
        path = [current]
        seen = {current.key}
        while current.parent_id:
            key = (current.parent_session_id, current.parent_id)
            if key in seen:
                break
            parent = by_key.get(key)
            if parent is None:
                getter = getattr(self.store, "get_node", None)
                parent = getter(*key) if callable(getter) else None
            if parent is None:
                break
            path.append(parent)
            seen.add(parent.key)
            current = parent
        path.reverse()
        return path

    def _switch_session(self, session_id: str, prompt: str) -> None:
        """Start projecting into a handoff session without ending the stream.

        A plan handoff can finish one run and immediately start a follow-up in
        an isolated session while reusing the same event callback.  The node
        writer is session-bound, so carry the transport emitter forward but
        reset the mutable projection state before creating the new turn.
        """

        if session_id == self.session_id:
            return
        if self.assistant is not None:
            self._seal_assistant("success")
        emit = self.writer.emit
        self.session_id = session_id
        self.prompt = prompt
        self.source_node_id = None
        self.writer = NodeWriter(self.store, emit=emit)
        self.parent = None
        self.assistant = None
        self.last_node = None
        self.assistant_blocks = []
        self.response_text = ""
        self.run_id = ""
        self.abort_category = None
        self.abort_code = ""
        self.terminal_error = None
        self.started = False
        self.closed = False
        self.start()

    def handle(self, event: Any) -> None:
        if self.closed or not self.started:
            return
        kind = getattr(event, "kind", "")
        if kind in _HIDDEN_RECOVERABLE_EVENTS:
            # Recoverable diagnostics stay out of user-facing nodes, but a
            # terminal error arriving immediately afterwards still needs the
            # most specific category for its visible terminal explanation.
            if kind == "tool_failed":
                self._remember_abort("tool", code="tool_failed")
            return
        message = str(getattr(event, "message", "") or "")
        data = getattr(event, "data", {})
        if not isinstance(data, Mapping):
            data = {}
        event_session_id = data.get("session_id")
        if isinstance(event_session_id, str) and event_session_id and event_session_id != self.session_id:
            self._switch_session(event_session_id, str(data.get("task") or self.prompt))
        if isinstance(data.get("run_id"), str) and data["run_id"]:
            self.run_id = str(data["run_id"])
        if kind in {"response_start", "thinking_start"}:
            self._ensure_assistant()
        elif kind in {"response_delta", "response"} and message:
            self._ensure_assistant()
            self.response_text += message
            self.assistant_blocks = [block for block in self.assistant_blocks if block.get("type") != "text"]
            self.assistant_blocks.append({"type": "text", "text": self.response_text})
            self._update_assistant()
        elif kind == "thinking_delta" and message:
            self._ensure_assistant()
            self.assistant_blocks.append({"type": "reasoning", "text": message})
            self._update_assistant()
        elif kind == "assistant_message":
            raw = data.get("message")
            if isinstance(raw, Mapping):
                blocks: list[dict[str, Any]] = []
                if isinstance(raw.get("reasoning"), str) and raw["reasoning"]:
                    blocks.append({"type": "reasoning", "text": raw["reasoning"]})
                if isinstance(raw.get("content"), str) and raw["content"]:
                    blocks.append({"type": "text", "text": raw["content"]})
                for tool in raw.get("tool_messages", []) if isinstance(raw.get("tool_messages"), list) else []:
                    if isinstance(tool, Mapping):
                        blocks.append(
                            {
                                "type": "tool_call",
                                "call_id": str(tool.get("call_id") or "call_unknown"),
                                "name": str(tool.get("name") or "unknown"),
                                "arguments": dict(tool.get("arguments") or {}),
                                "replay_safe": bool(tool.get("replay_safe", tool.get("retryable", True) is not False)),
                            }
                        )
                self._ensure_assistant()
                self.assistant_blocks = blocks
                self.response_text = str(raw.get("content") or "")
                self._update_assistant()
        elif kind == "tool_call":
            self._ensure_assistant()
            tool_name = str(data.get("tool") or data.get("name") or message or "unknown")
            replay_safe = data.get("replay_safe")
            if replay_safe is None:
                lowered = tool_name.lower()
                replay_safe = not any(token in lowered for token in ("bash", "shell", "command", "write", "mcp"))
            self.assistant_blocks.append(
                {
                    "type": "tool_call",
                    "call_id": str(data.get("call_id") or "call_unknown"),
                    "name": tool_name,
                    "arguments": dict(data.get("arguments") or {}),
                    "replay_safe": bool(replay_safe),
                }
            )
            self._update_assistant()
        elif kind == "tool_result":
            if self.assistant is not None:
                self._seal_assistant("success")
            tool_name = str(data.get("tool") or data.get("name") or "")
            lowered = tool_name.lower()
            replay_safe = data.get("replay_safe")
            if replay_safe is None:
                replay_safe = not any(token in lowered for token in ("bash", "shell", "command", "write", "mcp"))
            result_value = data.get("result", data.get("error", message))
            result_block: dict[str, Any] = {
                "type": "tool_result",
                "call_id": str(data.get("call_id") or "call_unknown"),
                "content": result_value,
                "status": "failed" if kind == "tool_failed" else "succeeded",
                "replay_safe": bool(replay_safe),
            }
            if tool_name:
                result_block["tool"] = tool_name
            if data.get("side_effect") is not None:
                result_block["side_effect"] = bool(data["side_effect"])
            result = self.writer.create(
                session_id=self.session_id,
                parent=self.last_node,
                data=message_payload("tool_result", result_block, **({"run_id": self.run_id} if self.run_id else {})),
                provider=self.provider,
                cwd=self.cwd,
            )
            self.last_node = self.writer.delete(
                result.session_id, result.id, status="success" if kind == "tool_result" else "failed"
            )
        elif kind == "model_error":
            self._remember_abort(self._model_error_category(data), code=str(data.get("error_type") or "model_error"))
        elif kind == "error":
            if data.get("unexpected"):
                category = self.abort_category or self._data_category(data)
                if category is None:
                    self.finish("failed")
                else:
                    self.finish(
                        "abort",
                        message,
                        category=category,
                        code=self.abort_code or str(data.get("error_type") or "runtime_error"),
                    )
            else:
                self.finish(
                    "abort",
                    message,
                    category=self.abort_category or "agent",
                    code=self.abort_code or str(data.get("error_type") or "agent_error"),
                )
        elif kind in {"cancelled", "run_suspended"}:
            stop_reason = str(data.get("stop_reason") or data.get("reason") or "")
            self.finish("abort", message, category="user", code=stop_reason or "user_cancelled")
        elif kind in {"approval_requested", "approval_granted"}:
            self._append_event_block("approval", kind, message, data)
        elif kind in {"user_input_requested", "user_input_received"}:
            self._append_event_block("question", kind, message, data)
        elif kind in {
            "plan",
            "plan_progress",
            "replan_requested",
            "replan_applied",
            "feedback_received",
            "handoff_created",
            "steering_received",
            "steering_applied",
            "model_repair",
            "strategy",
        }:
            self._append_event_block("plan", kind, message, data)
        elif kind == "skills_selected":
            self._append_event_block("skill_snapshot", kind, message, data)
        elif kind in {
            "subagent_queued",
            "subagent_started",
            "subagent_write_requested",
            "subagent_completed",
            "subagent_failed",
            "subagent_indeterminate",
        }:
            self._append_event_block("subagent", kind, message, data)
        elif kind == "context_compaction_completed":
            # A compaction summary is a first-class node, not a mutable flag on
            # the preceding assistant message.  Keep the original ancestors
            # intact and advance the active parent to the summary node.
            self._seal_assistant("success")
            summary = str(data.get("summary") or message or "")
            source_ids = data.get("source_ids", [])
            if not isinstance(source_ids, list):
                source_ids = []
            path = self._ancestor_path(self.last_node)
            if not source_ids:
                old_compaction = self.last_node.compactionIdx if self.last_node is not None else ""
                start = next((index for index, item in enumerate(path) if item.id == old_compaction), 0)
                source_ids = [item.id for item in path[start:]]
            first_kept = path[max(0, len(path) - DEFAULT_COMPACTION_RETENTION)].id if path else None
            node = self.writer.create(
                session_id=self.session_id,
                parent=self.last_node,
                data=compaction_payload(summary, source_ids=[str(item) for item in source_ids]),
                provider=self.provider,
                cwd=self.cwd,
                first_kept_entry_id=first_kept,
                # An empty explicit value tells RuntimeState to point the
                # compaction index at this newly created summary node.
                compaction_idx="",
            )
            self.last_node = self.writer.delete(node.session_id, node.id, status="success")

    def handle_input(self, payload: Mapping[str, Any]) -> None:
        """Store approval/question prompts as canonical content blocks."""

        if self.closed:
            return
        kind = str(payload.get("kind") or "approval")
        block_type = "question" if kind == "question" else "approval"
        block: dict[str, Any] = {"type": block_type, "event": "decision_requested", **dict(payload.get("data") or {})}
        if isinstance(payload.get("message"), str):
            block.setdefault("text", payload["message"])
        self._ensure_assistant()
        self.assistant_blocks.append(block)
        self._update_assistant()

    def finish(
        self,
        status: NodeStatus,
        final_answer: str = "",
        *,
        category: TerminalErrorCategory | None = None,
        code: str = "",
    ) -> RuntimeState | None:
        if self.closed:
            return self.last_node
        if status != "success":
            self.terminal_error = terminal_error_payload(
                status,
                category if status == "abort" else None,
                code=code or self.abort_code or None,
                detail=final_answer if status == "abort" else None,
            )
            final_answer = terminal_error_text(self.terminal_error)
        if self.assistant is None and (final_answer or status != "success"):
            self._ensure_assistant()
            self.assistant_blocks = [{"type": "text", "text": final_answer}] if final_answer else []
            if status != "success" and not self.assistant_blocks:
                self.assistant_blocks = [{"type": "text", "text": "Execution did not complete."}]
            self._update_assistant()
        elif self.assistant is not None and final_answer:
            # A plan/control-only run may never emit response_delta or an
            # assistant_message event.  The terminal run result is still the
            # canonical assistant text and must not be lost merely because a
            # dynamic node already contains approval/plan blocks.
            self.assistant_blocks = [block for block in self.assistant_blocks if block.get("type") != "text"]
            self.assistant_blocks.append({"type": "text", "text": final_answer})
            self._update_assistant()
        if self.assistant is not None:
            self._seal_assistant(status)
        self.closed = True
        return self.last_node

    def finish_exception(self, error: BaseException) -> RuntimeState | None:
        """Finish an uncaught worker exception without losing its category."""

        category = self.abort_category or self._exception_category(error)
        if category is None:
            return self.finish("failed")
        return self.finish(
            "abort",
            str(error),
            category=category,
            code=self.abort_code or error.__class__.__name__,
        )


__all__ = ["RuntimeEventNodeBridge"]
