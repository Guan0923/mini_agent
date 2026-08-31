"""RuntimeEvent-to-Item projection and compaction event handling."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from backend.domain.runtime_state import RuntimeState, RuntimeStateTree, TerminalErrorCategory

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
_TOOL_ERROR_TYPES = frozenset({"ToolError", "ConfirmationRequired"})
_HIDDEN_RECOVERABLE_EVENTS = frozenset({"tool_recovery", "model_repair", "model_retry"})


class _EventProjectionMixin:
    @staticmethod
    def _error_category(data: Mapping[str, Any]) -> TerminalErrorCategory:
        error_type = str(data.get("error_type") or "")
        lowered = error_type.lower()
        if error_type in _NETWORK_ERROR_TYPES or any(
            token in lowered for token in ("network", "timeout", "transport", "connection")
        ):
            return "network"
        if error_type in _TOOL_ERROR_TYPES or "tool" in lowered:
            return "tool"
        return "agent"

    @classmethod
    def _exception_category(cls, error: BaseException) -> TerminalErrorCategory:
        return cls._error_category({"error_type": error.__class__.__name__})

    def _event_item(self, item_type: str, kind: str, message: str, data: Mapping[str, Any]) -> None:
        item = {str(key): self._json_value(value) for key, value in data.items()}
        item.update({"type": item_type, "event": kind, "status": "success"})
        if message:
            item.setdefault("text", message)
        self._append_item(item)

    def _set_tool_call_status(self, call_id: str, status: str) -> None:
        if self.assistant is None:
            return
        data_idx = self.assistant.current_data_idx
        messages = self.assistant.data[data_idx]
        for message_idx in range(len(messages) - 1, -1, -1):
            message = messages[message_idx]
            if message.get("role") != "assistant":
                continue
            items = message.get("content", [])
            for item_idx in range(len(items) - 1, -1, -1):
                item = items[item_idx]
                if item.get("type") != "tool_call" or item.get("call_id") != call_id:
                    continue
                if item.get("status") == status:
                    return
                self.assistant = self.writer.set_item_status(
                    self.assistant,
                    data_idx=data_idx,
                    message_idx=message_idx,
                    item_idx=item_idx,
                    status=status,
                )
                if message_idx == self.assistant_message_idx and item_idx < len(self.assistant_blocks):
                    self.assistant_blocks[item_idx]["status"] = status
                self.last_node = self.assistant
                self._record_completed_item(message_idx, item_idx)
                return

    def _settle_running_items(self, status: str) -> None:
        if self.assistant is None:
            return
        data_idx = self.assistant.current_data_idx
        targets = [
            (message_idx, item_idx)
            for message_idx, message in enumerate(self.assistant.data[data_idx])
            for item_idx, item in enumerate(message.get("content", []))
            if item.get("status") == "running"
        ]
        for message_idx, item_idx in targets:
            self.assistant = self.writer.set_item_status(
                self.assistant,
                data_idx=data_idx,
                message_idx=message_idx,
                item_idx=item_idx,
                status=status,
            )
            if message_idx == self.assistant_message_idx and item_idx < len(self.assistant_blocks):
                self.assistant_blocks[item_idx]["status"] = status
            self._record_completed_item(message_idx, item_idx)
        self.last_node = self.assistant

    def _tool_result(self, message: str, data: Mapping[str, Any], *, status: str) -> None:
        tool = str(data.get("tool") or data.get("name") or "")
        call_id = str(data.get("call_id") or "call_unknown")
        self._set_tool_call_status(call_id, status)
        result = {
            "type": "tool_result",
            "call_id": call_id,
            "content": self._json_value(data.get("result", data.get("error", message))),
            "status": status,
            "replay_safe": bool(
                data.get(
                    "replay_safe", not any(x in tool.lower() for x in ("write", "bash", "shell", "command", "mcp"))
                )
            ),
        }
        if tool:
            result["tool"] = tool
        if isinstance(data.get("failure_code"), str):
            result["failure_code"] = str(data["failure_code"])
        if isinstance(data.get("retryable"), bool):
            result["retryable"] = bool(data["retryable"])
        self._append_item(result)

    def handle(self, event: Any) -> None:
        if self.closed or not self.started:
            return
        kind = str(getattr(event, "kind", "") or "")
        message = str(getattr(event, "message", "") or "")
        data = getattr(event, "data", {})
        if not isinstance(data, Mapping):
            data = {}
        if kind in _HIDDEN_RECOVERABLE_EVENTS:
            return
        if isinstance(data.get("run_id"), str):
            self.run_id = str(data["run_id"])
        config = data.get("runtime_config") or data.get("config")
        if isinstance(config, Mapping):
            self.apply_runtime_config(config)
        usage = data.get("node_usage") if isinstance(data.get("node_usage"), Mapping) else data.get("usage")
        if isinstance(usage, Mapping):
            self._apply_usage(usage)
        if kind == "thinking_start":
            self._start_assistant_after_report()
            self._begin_stream_item("reasoning")
        elif kind == "thinking_delta":
            self._update_stream_item("reasoning", message)
        elif kind == "thinking_end":
            self._finish_stream_item("reasoning")
        elif kind == "response_start":
            self._start_assistant_after_report()
            self._begin_stream_item("text")
        elif kind == "response_delta":
            self._update_stream_item("text", message)
        elif kind == "response_end":
            self._finish_stream_item("text")
        elif kind == "assistant_message" and isinstance(data.get("message"), Mapping):
            self._start_assistant_after_report()
            raw = data["message"]
            items: list[dict[str, Any]] = []
            if raw.get("reasoning") and not data.get("reasoning_streamed"):
                items.append({"type": "reasoning", "text": str(raw["reasoning"]), "status": "success"})
            if raw.get("content") and not data.get("content_streamed"):
                items.append({"type": "text", "text": str(raw["content"]), "status": "success"})
            for tool in raw.get("tool_messages", []) if isinstance(raw.get("tool_messages"), list) else []:
                call_id = str(tool.get("call_id") or "call_unknown") if isinstance(tool, Mapping) else ""
                if isinstance(tool, Mapping) and not any(
                    item.get("type") == "tool_call" and item.get("call_id") == call_id for item in self.assistant_blocks
                ):
                    items.append(
                        {
                            "type": "tool_call",
                            "call_id": call_id,
                            "name": str(tool.get("name") or "unknown"),
                            "arguments": dict(tool.get("arguments") or {}),
                            "replay_safe": bool(tool.get("replay_safe", True)),
                            "status": "running",
                        }
                    )
            self._append_items(items)
        elif kind == "tool_call":
            call_id = str(data.get("call_id") or "call_unknown")
            if not any(
                item.get("type") == "tool_call" and item.get("call_id") == call_id for item in self.assistant_blocks
            ):
                name = str(data.get("tool") or data.get("name") or message or "unknown")
                self._append_item(
                    {
                        "type": "tool_call",
                        "call_id": call_id,
                        "name": name,
                        "arguments": dict(data.get("arguments") or {}),
                        "status": "running",
                        "replay_safe": bool(
                            data.get(
                                "replay_safe",
                                not any(x in name.lower() for x in ("write", "bash", "shell", "command", "mcp")),
                            )
                        ),
                    }
                )
        elif kind == "tool_result":
            self._tool_result(message, data, status="success")
        elif kind == "tool_failed":
            self.abort_category = "tool"
            self._tool_result(message, data, status="failed")
        elif kind == "model_error":
            self.abort_category = self._error_category(data)
            self.abort_code = str(data.get("error_type") or "model_error")
        elif kind in {"approval_requested", "approval_granted"}:
            # Tool approval lifecycle events remain in the Runtime log.  The
            # durable Turn stores only the interactive decision Item emitted
            # by ``handle_input`` so one approval cannot become three
            # identical assistant Items.
            if not data.get("tool"):
                self._event_item("approval", kind, message, data)
        elif kind in {"user_input_requested", "user_input_received"}:
            self._event_item("question", kind, message, data)
        elif kind == "steering_applied":
            self._append_steering_message(data)
        elif kind == "steering_received":
            return
        elif kind == "subagent_report":
            self._append_subagent_report(data)
        elif kind in {"plan", "feedback_received", "handoff_created"}:
            self._event_item("plan", kind, message, data)
        elif kind == "skills_selected":
            self._event_item("skill_snapshot", kind, message, data)
        elif kind.startswith("subagent_"):
            self._event_item("subagent", kind, message, data)
        elif kind == "context_compaction_completed":
            self._begin_compact_turn(str(data.get("summary") or message or ""))
        elif kind in {"cancelled", "run_suspended"}:
            self.finish("paused", message or "Paused by user.", category="user")
        elif kind == "error":
            self.finish(
                "failed", message or "Execution failed.", category=self.abort_category or self._error_category(data)
            )

    def _begin_compact_turn(self, summary: str) -> RuntimeState:
        source = self.assistant or self.last_node
        if source is None:
            raise RuntimeError("Compaction requires an active Turn.")
        if source.status == "running":
            source = self.writer.finalize(source, "success")
        creator = getattr(self.store, "create_compact_turn", None)
        if callable(creator):
            compacted = creator(source.id, summary, new_turn_id=self.compaction_turn_id)
            compacted = self.writer.snapshot(compacted)
        else:
            compacted = self.writer.create(
                RuntimeStateTree(self.store.load_nodes(source.session_id)).compact(
                    source,
                    summary,
                    id=self.compaction_turn_id,
                )
            )
        self.assistant = compacted
        self.last_node = compacted
        self.turn_id = compacted.id
        self.assistant_blocks = compacted.assistant_items
        self.assistant_message_idx = len(compacted.data[compacted.current_data_idx]) - 1
        self.protected_item_count = len(self.assistant_blocks)
        self._stream_item_index = None
        self._stream_item_type = None
        self._stream_text = ""
        self.produced_item = bool(self.assistant_blocks)
        return compacted

    def handle_input(self, payload: Mapping[str, Any]) -> None:
        kind = str(payload.get("kind") or "approval")
        self._event_item(
            "question" if kind == "question" else "approval",
            "decision_requested",
            str(payload.get("message") or ""),
            dict(payload.get("data") or {}),
        )
