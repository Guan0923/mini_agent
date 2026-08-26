"""Project internal RuntimeEvents into one durable Turn per interaction."""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from backend.domain.runtime_state import (
    NodeFrame,
    NodeStatus,
    NodeWriter,
    RuntimeNodeStore,
    RuntimeState,
    RuntimeStateTree,
    RuntimeStateValidationError,
    TerminalErrorCategory,
    terminal_error_payload,
)
from backend.providers.token_usage import normalize_provider_usage

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
_HIDDEN_RECOVERABLE_EVENTS = frozenset({"tool_recovery", "model_repair", "model_retry"})


class RuntimeEventNodeBridge:
    """Keep the canonical Turn synchronized with an in-process AgentRuntime."""

    def __init__(
        self,
        store: RuntimeNodeStore,
        *,
        session_id: str,
        prompt: str,
        turn_id: str | None = None,
        thread_id: str | None = None,
        source_node_id: str | None = None,
        adopt_existing: bool = False,
        user: str = "",
        provider: str = "unknown",
        provider_name: str | None = None,
        model: str = "unknown",
        model_config: Mapping[str, Any] | None = None,
        permission_mode: str = "read_only",
        running_mode: str = "agent",
        cwd: str = "",
        thinking_level: str = "medium",
        references: Sequence[Mapping[str, str]] | None = None,
        emit: Callable[[NodeFrame], None],
    ) -> None:
        self.store = store
        self.session_id = session_id
        self.thread_id = thread_id or session_id
        self.turn_id = turn_id
        self.prompt = prompt
        self.source_node_id = source_node_id
        self.adopt_existing = adopt_existing
        self.user = user
        self.provider = provider or "unknown"
        self.provider_name = provider_name or self.provider
        self.model = model or "unknown"
        snapshot = dict(model_config or {})
        snapshot.setdefault("current_model", self.model)
        snapshot.setdefault("reasoning_effort", thinking_level or "medium")
        snapshot.setdefault("thinking", "enable")
        snapshot.setdefault("context_length", 128000)
        snapshot.setdefault("output_length", 8192)
        snapshot.setdefault("temperature", 1.0)
        self.model_config = snapshot
        self.permission_mode = permission_mode
        self.running_mode = running_mode
        self.cwd = cwd
        self.references = [dict(item) for item in references or []]
        self.writer = NodeWriter(store, emit=emit)
        self.parent: RuntimeState | None = None
        self.assistant: RuntimeState | None = None
        self.last_node: RuntimeState | None = None
        self.assistant_blocks: list[dict[str, Any]] = []
        self._stream_item_index: int | None = None
        self._stream_item_type: str | None = None
        self._stream_text = ""
        self.protected_item_count = 0
        self.run_id = ""
        self.abort_category: TerminalErrorCategory | None = None
        self.abort_code = ""
        self.terminal_error: dict[str, Any] | None = None
        self.persistence_failed = False
        self.produced_item = False
        self.started = False
        self.closed = False
        self.runtime: Any = None

    def bind_runtime(self, runtime: Any) -> None:
        self.runtime = runtime
        runtime.state.provider_name = self.provider_name
        runtime.state.provider = self.provider
        runtime.state.model = str(self.model_config.get("current_model") or self.model)
        runtime.state.model_snapshot = dict(self.model_config)
        runtime.state.permission_mode = self.permission_mode
        runtime.state.running_mode = self.running_mode
        runtime.services.runtime_node_event = self.handle
        runtime.services.runtime_node_context = self.model_context

    def model_context(self) -> list[RuntimeState]:
        current = self._current()
        if current is None:
            return []
        try:
            return RuntimeStateTree(self.store.load_nodes(current.session_id)).model_input(current)
        except (KeyError, RuntimeError, ValueError):
            return [current]

    def _current(self) -> RuntimeState | None:
        target = self.assistant or self.last_node
        if target is None:
            return None
        try:
            return self.writer.current(target.session_id, target.id)
        except KeyError:
            return target.clone()

    def _latest_parent(self) -> RuntimeState | None:
        nodes = [node for node in self.store.load_nodes(self.session_id) if node.thread_id == self.thread_id]
        if not nodes:
            return None
        parent_keys = {(node.parent_session_id, node.parent_id) for node in nodes if node.parent_id}
        leaves = [node for node in nodes if node.key not in parent_keys]
        return max(leaves or nodes, key=lambda node: (node.timestamp, node.id))

    def start(self) -> RuntimeState:
        if self.started:
            current = self._current()
            if current is None:
                raise RuntimeError("Turn bridge has no active Turn.")
            return current
        if self.source_node_id:
            source = self.store.get_node(self.session_id, self.source_node_id)
            if source is None:
                raise ValueError("Unknown source Turn.")
            if source.session_id != self.session_id:
                raise ValueError("A Turn cannot continue across Sessions.")
            if self.adopt_existing or not self.prompt:
                if source.status == "paused":
                    resume = getattr(self.store, "resume_turn_node", None)
                    if not callable(resume):
                        raise RuntimeError("The Turn store does not support resume.")
                    source = resume(source.id)
                elif source.status != "running":
                    raise ValueError("Only a paused or running Turn can resume in place.")
                source = self.writer.snapshot(source)
                self.assistant = source
                self.last_node = source
                self.thread_id = source.thread_id
                self.turn_id = source.id
                self.assistant_blocks = source.assistant_items
                if self.assistant_blocks and self.assistant_blocks[0].get("type") == "compaction":
                    self.protected_item_count = 1 + int(self.assistant_blocks[0].get("kept_item_count") or 0)
                self.started = True
                return source
            self.parent = source
            if self.thread_id == self.session_id:
                self.thread_id = source.thread_id
        else:
            self.parent = self._latest_parent()
        user_item: dict[str, Any] = {"type": "text", "text": self.prompt}
        if self.references:
            user_item["references"] = self.references
        node = RuntimeState.create(
            session_id=self.session_id,
            thread_id=self.thread_id,
            id=self.turn_id,
            parent=self.parent,
            user_content=[user_item],
            user=self.user,
            provider_name=self.provider_name,
            model=self.model_config,
            permission_mode=self.permission_mode,
            running_mode=self.running_mode,
            cwd=self.cwd,
        )
        node = self.writer.create(node)
        self.assistant = node
        self.last_node = node
        self.turn_id = node.id
        self.started = True
        return node

    def start_for_compaction(self) -> RuntimeState | None:
        if not self.started:
            self.parent = (
                self.store.get_node(self.session_id, self.source_node_id)
                if self.source_node_id
                else self._latest_parent()
            )
            self.last_node = self.parent
            self.assistant = self.parent if self.parent and self.parent.status == "running" else None
            self.started = True
        return self.last_node

    @staticmethod
    def _json_value(value: Any) -> Any:
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

    def _begin_stream_item(self, item_type: str) -> None:
        if item_type not in {"reasoning", "text"}:
            raise ValueError("Only reasoning and text Items can stream.")
        if self._stream_item_type == item_type:
            return
        self._finish_stream_item()
        self._stream_item_type = item_type
        self._stream_item_index = None
        self._stream_text = ""

    def _update_stream_item(self, item_type: str, chunk: str) -> None:
        if not chunk:
            return
        if self._stream_item_type != item_type:
            self._begin_stream_item(item_type)
        if self._stream_item_index is None:
            self._stream_item_index = len(self.assistant_blocks)
            self._stream_text = chunk
            item = {"type": item_type, "text": chunk}
            self.assistant_blocks.append(item)
            assert self.assistant is not None
            self.assistant = self.writer.append_item(self.assistant, item, persist=False)
        else:
            self._stream_text += chunk
            self.assistant_blocks[self._stream_item_index] = {"type": item_type, "text": self._stream_text}
            assert self.assistant is not None
            self.assistant = self.writer.append_text(
                self.assistant,
                data_idx=self.assistant.current_data_idx,
                item_idx=self._stream_item_index,
                delta=chunk,
            )
        self.last_node = self.assistant

    def _finish_stream_item(self, item_type: str | None = None) -> None:
        if self._stream_item_type is None:
            return
        if item_type is not None and self._stream_item_type != item_type:
            return
        if self._stream_item_index is not None:
            assert self.assistant is not None
            self.assistant = self.writer.persist(self.assistant)
            self.last_node = self.assistant
            self.produced_item = True
        self._stream_item_index = None
        self._stream_item_type = None
        self._stream_text = ""

    def _append_item(self, item: Mapping[str, Any], *, persist: bool = True) -> RuntimeState:
        self._finish_stream_item()
        normalized = {str(key): self._json_value(value) for key, value in item.items()}
        self.assistant_blocks.append(normalized)
        if self.assistant is None:
            raise RuntimeError("No active Turn.")
        updated = self.writer.append_item(self.assistant, normalized, persist=persist)
        self.assistant = updated
        self.last_node = updated
        if persist:
            self.produced_item = True
        return updated

    def _append_items(self, items: Sequence[Mapping[str, Any]]) -> RuntimeState | None:
        self._finish_stream_item()
        if not items:
            return self.assistant
        normalized = [{str(key): self._json_value(value) for key, value in item.items()} for item in items]
        self.assistant_blocks.extend(normalized)
        if self.assistant is None:
            raise RuntimeError("No active Turn.")
        updated = self.writer.append_items(self.assistant, normalized, persist=True)
        self.assistant = updated
        self.last_node = updated
        self.produced_item = True
        return updated

    def _apply_usage(self, raw: Any) -> None:
        if not isinstance(raw, Mapping) or self.assistant is None:
            return
        normalized = normalize_provider_usage(raw)
        current = self.writer.current(self.assistant.session_id, self.assistant.id)
        merged = {key: value if value is not None else current.usage.get(key) for key, value in normalized.items()}
        self.assistant = self.writer.update_config(current, usage=merged)
        self.last_node = self.assistant

    def apply_runtime_config(self, config: Mapping[str, Any]) -> RuntimeState | None:
        provider_name = str(config.get("provider_name") or self.provider_name)
        if not provider_name.strip():
            raise RuntimeStateValidationError("provider_name must be a non-empty string.")
        model = dict(self.model_config)
        if isinstance(config.get("model"), Mapping):
            model.update(dict(config["model"]))
        permission = str(config.get("permission_mode") or self.permission_mode)
        running = str(config.get("running_mode") or self.running_mode)
        if permission not in {"read_only", "workspace_write", "full_access"}:
            raise RuntimeStateValidationError("permission_mode must be read_only, workspace_write, or full_access.")
        if running not in {"agent", "plan"}:
            raise RuntimeStateValidationError("running_mode must be agent or plan.")
        self.provider_name, self.model_config = provider_name, model
        self.permission_mode, self.running_mode = permission, running
        if self.assistant is None:
            return self.last_node
        self.assistant = self.writer.update_config(
            self.assistant, provider_name=provider_name, model=model, permission_mode=permission, running_mode=running
        )
        self.last_node = self.assistant
        if self.runtime is not None:
            pending = dict(self.runtime.services.pending_runtime_config or {})
            pending.update(dict(config))
            self.runtime.services.pending_runtime_config = pending
        return self.assistant

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
        item.update({"type": item_type, "event": kind})
        if message:
            item.setdefault("text", message)
        self._append_item(item)

    def _tool_result(self, message: str, data: Mapping[str, Any], *, status: str) -> None:
        tool = str(data.get("tool") or data.get("name") or "")
        result = {
            "type": "tool_result",
            "call_id": str(data.get("call_id") or "call_unknown"),
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
            self._begin_stream_item("reasoning")
        elif kind == "thinking_delta":
            self._update_stream_item("reasoning", message)
        elif kind == "thinking_end":
            self._finish_stream_item("reasoning")
        elif kind == "response_start":
            self._begin_stream_item("text")
        elif kind == "response_delta":
            self._update_stream_item("text", message)
        elif kind == "response_end":
            self._finish_stream_item("text")
        elif kind == "assistant_message" and isinstance(data.get("message"), Mapping):
            raw = data["message"]
            items: list[dict[str, Any]] = []
            if raw.get("reasoning") and not data.get("reasoning_streamed"):
                items.append({"type": "reasoning", "text": str(raw["reasoning"])})
            if raw.get("content") and not data.get("content_streamed"):
                items.append({"type": "text", "text": str(raw["content"])})
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
                        "replay_safe": bool(
                            data.get(
                                "replay_safe",
                                not any(x in name.lower() for x in ("write", "bash", "shell", "command", "mcp")),
                            )
                        ),
                    }
                )
        elif kind == "tool_result":
            self._tool_result(message, data, status="succeeded")
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
        elif kind in {"plan", "feedback_received", "handoff_created", "steering_received", "steering_applied"}:
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
        if self.assistant is None:
            raise RuntimeError("Compaction requires an active Turn.")
        source = self.writer.finalize(self.assistant, "success")
        creator = getattr(self.store, "create_compact_turn", None)
        if callable(creator):
            compacted = creator(source.id, summary)
            compacted = self.writer.snapshot(compacted)
        else:
            compacted = self.writer.create(
                RuntimeStateTree(self.store.load_nodes(source.session_id)).compact(source, summary)
            )
        self.assistant = compacted
        self.last_node = compacted
        self.turn_id = compacted.id
        self.assistant_blocks = compacted.assistant_items
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
        if status not in {"success", "paused", "failed"}:
            raise ValueError("A Turn can only finish as success, paused, or failed.")
        if self.assistant is None:
            self.start()
        if (
            final_answer
            and status == "success"
            and not any(item.get("type") == "text" for item in self.assistant_blocks)
        ):
            self._append_item({"type": "text", "text": final_answer})
        if status in {"paused", "failed"}:
            retryable = status == "paused"
            self.terminal_error = terminal_error_payload(
                category or ("user" if retryable else "agent"),
                final_answer or "Execution did not complete.",
                retryable=retryable,
            )
            self._append_item(self.terminal_error)
        try:
            assert self.assistant is not None
            self.last_node = self.writer.finalize(self.assistant, status)
            self.assistant = self.last_node
        except Exception:
            self.persistence_failed = True
            self.closed = True
            return None
        self.closed = True
        return self.last_node

    def preserve_placeholder(self, *, code: str = "runtime_exception") -> RuntimeState | None:
        return self.finish("failed", "Execution failed.", category="agent", code=code)

    def finish_exception(self, error: BaseException) -> RuntimeState | None:
        category = self.abort_category or self._exception_category(error)
        if category == "network" and not self.produced_item:
            self.terminal_error = terminal_error_payload(
                "network", str(error) or "Network unavailable.", retryable=True
            )
            self.closed = True
            return self._current()
        status: NodeStatus = "paused" if category == "network" and self.produced_item else "failed"
        return self.finish(status, str(error) or "Execution failed.", category=category, code=error.__class__.__name__)


__all__ = ["RuntimeEventNodeBridge"]
