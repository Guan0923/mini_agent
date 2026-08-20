"""Bridge the legacy execution callbacks into RuntimeState node frames.

This is a migration seam: the runner may still report internal callbacks while
clients only receive the canonical create/update/delete protocol.  No legacy
event object is persisted or sent over SSE.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Literal

from backend.domain.runtime_state import (
    DEFAULT_COMPACTION_RETENTION,
    NodeFrame,
    NodeStatus,
    NodeWriter,
    RuntimeNodeStore,
    RuntimeState,
    RuntimeStateTree,
    RuntimeStateValidationError,
    TerminalErrorCategory,
    compaction_payload,
    message_payload,
    terminal_error_payload,
    terminal_error_text,
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
_HIDDEN_RECOVERABLE_EVENTS = frozenset({"tool_failed", "tool_recovery", "model_repair", "model_retry"})


class RuntimeEventNodeBridge:
    """Project execution events into durable canonical message nodes."""

    def __init__(
        self,
        store: RuntimeNodeStore,
        *,
        session_id: str,
        prompt: str,
        source_node_id: str | None = None,
        allow_branch: bool = False,
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
        self.prompt = prompt
        self.source_node_id = source_node_id
        self.allow_branch = allow_branch
        self.user = user
        # ``provider_name`` is the user-owned configuration identity.  Keep
        # the internal adapter kind in ``provider`` so a named configuration
        # such as ``work-openai`` never masquerades as a protocol provider.
        self.provider_name = provider_name or provider or "unknown"
        self.provider = provider or "unknown"
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
        self.thinking_level = thinking_level or "medium"
        # Structured file references travel as user-message metadata on the
        # canonical user node.  They are deliberately never injected into the
        # prompt text; the agent reads referenced files through its tools.
        self.references = [dict(item) for item in references or []]
        self.writer = NodeWriter(store, emit=emit)
        self.parent: RuntimeState | None = None
        self.assistant: RuntimeState | None = None
        self.last_node: RuntimeState | None = None
        self.assistant_blocks: list[dict[str, Any]] = []
        # Call IDs declared by the current assistant response. Per-tool
        # lifecycle events may arrive after the first result seals the
        # dynamic assistant, so this set keeps the whole batch together.
        self.batch_call_ids: set[str] = set()
        self.response_text = ""
        self.run_id = ""
        self.abort_category: TerminalErrorCategory | None = None
        self.abort_code = ""
        self.terminal_error: dict[str, str] | None = None
        # ``True`` means the dynamic copy could not be atomically sealed.  The
        # durable failed placeholder is intentionally left untouched so a
        # subsequent resume can recover the turn without pretending that the
        # streamed contents were durably committed.
        self.persistence_failed = False
        self.started = False
        self.closed = False
        self.runtime = None

    def bind_runtime(self, runtime: Any) -> None:
        """Connect the node bridge to the ephemeral AgentRuntime sidecar."""

        self.runtime = runtime
        runtime.state.provider_name = self.provider_name
        runtime.state.provider = self.provider
        runtime.state.model = str(self.model_config.get("current_model") or self.model)
        runtime.state.model_snapshot = dict(self.model_config)
        runtime.state.permission_mode = self.permission_mode
        runtime.state.running_mode = self.running_mode
        runtime.services.runtime_node_event = self.handle
        # Model requests must read the canonical path, not the legacy
        # RuntimeState.messages transcript.  The callback is intentionally
        # lazy: the dynamic assistant sidecar changes while a response/tool
        # call is in flight and every subsequent boundary must observe it.
        runtime.services.runtime_node_context = self.model_context

    def model_context(self) -> list[RuntimeState]:
        """Return durable ancestors plus the authoritative dynamic leaf."""

        current = self._dynamic_current() or self.last_node
        if current is None:
            return []
        path = self._ancestor_path(current)
        dynamic = self._dynamic_current() or self.last_node
        if dynamic is not None:
            for index, item in enumerate(path):
                if item.key == dynamic.key:
                    path[index] = dynamic.clone()
                    break
            else:
                path.append(dynamic.clone())
        # The same identity can occur as a persisted failed placeholder and a
        # dynamic sidecar.  Keep one entry and let the sidecar win.
        unique: dict[tuple[str, str], RuntimeState] = {}
        for item in path:
            unique[item.key] = item
        # Reuse the domain compaction/window algorithm so an automatic or
        # manual summary node supersedes older raw ancestors consistently for
        # every provider protocol.
        values = list(unique.values())
        try:
            context = RuntimeStateTree(values).model_input(dynamic)
        except (KeyError, RuntimeError, ValueError):
            context = values
        result: list[RuntimeState] = []
        for item in context:
            if not item.data:
                continue
            # The assistant placeholder exists so a running PATCH has a
            # dynamic target, but an empty assistant message is not a model
            # turn and must not be serialized into the next request.
            if item.data.get("type") == "message":
                message = item.data.get("message")
                if (
                    isinstance(message, Mapping)
                    and message.get("role") == "assistant"
                    and not message.get("content")
                    and not message.get("error")
                ):
                    continue
            result.append(item)
        return result

    def _dynamic_current(self) -> RuntimeState | None:
        """Read the mutable assistant sidecar after a writer update."""

        if self.assistant is None:
            return None
        try:
            return self.writer.current(self.assistant.session_id, self.assistant.id)
        except KeyError:
            return self.assistant.clone()

    @staticmethod
    def _usage_snapshot(raw: Mapping[str, Any]) -> dict[str, int | None]:
        """Map provider-specific usage keys to the five node fields."""
        return normalize_provider_usage(raw)

    def _apply_usage(self, raw: Any) -> None:
        if not isinstance(raw, Mapping):
            return
        target = self.assistant
        if target is None:
            return
        # A provider may report usage in more than one event (for example a
        # streamed response followed by a final response envelope).  Merge
        # only fields that are actually known so a later partial payload never
        # erases an earlier authoritative value with null.
        normalized = self._usage_snapshot(raw)
        current = self.writer.current(target.session_id, target.id).usage
        merged = {key: (value if value is not None else current.get(key)) for key, value in normalized.items()}
        self.writer.update_config(target, usage=merged)

    def apply_runtime_config(self, config: Mapping[str, Any]) -> RuntimeState | None:
        """Apply the latest user-selected config at a safe event boundary."""

        # Stage and validate the whole candidate before mutating either the
        # bridge or the dynamic node.  A malformed partial PATCH must be
        # atomic: it cannot poison the bridge's next-boundary configuration.
        raw_provider = config.get("provider_name")
        if raw_provider is not None and (not isinstance(raw_provider, str) or not raw_provider.strip()):
            raise RuntimeStateValidationError("provider_name must be a non-empty string.")
        candidate_provider_name = self.provider_name if raw_provider is None else str(raw_provider).strip()
        provider_changed = candidate_provider_name.casefold() != self.provider_name.casefold()
        candidate_model = dict(self.model_config)
        if provider_changed:
            # A provider name identifies a complete saved configuration.  Do
            # not carry model limits or thinking settings across a switch,
            # even for an embedding caller without an authenticated resolver.
            candidate_model = {
                "reasoning_effort": "medium",
                "current_model": "unknown",
                "context_length": 128000,
                "output_length": 8192,
                "thinking": "enable",
                "temperature": 1.0,
            }
            resolver = getattr(getattr(self.runtime, "services", None), "provider_config_resolver", None)
            if callable(resolver):
                resolved = resolver(candidate_provider_name)
                candidate_model.update(
                    {
                        "current_model": getattr(resolved, "model", None) or "unknown",
                        "context_length": int(getattr(resolved, "context_size", None) or 128000),
                        "output_length": int(getattr(resolved, "max_tokens", None) or 8192),
                    }
                )
        if isinstance(config.get("model"), Mapping):
            candidate_model.update(dict(config["model"]))
        candidate_permission = (
            self.permission_mode if config.get("permission_mode") is None else str(config["permission_mode"])
        )
        candidate_running = self.running_mode if config.get("running_mode") is None else str(config["running_mode"])
        if candidate_permission not in {"approval_for_me", "read_only", "workspace_write", "full_access"}:
            raise RuntimeStateValidationError("permission_mode must be read_only, workspace_write, or full_access.")
        if candidate_running not in {"agent", "plan"}:
            raise RuntimeStateValidationError("running_mode must be agent or plan.")
        updated: RuntimeState | None = self.last_node
        if self.assistant is not None:
            updated = self.writer.update_config(
                self.assistant,
                provider_name=candidate_provider_name,
                model=candidate_model,
                permission_mode=candidate_permission,
                running_mode=candidate_running,
            )
        else:
            # Before ``start`` there is no dynamic leaf to update.  Construct a
            # throwaway domain node solely to apply the same protocol checks.
            RuntimeState.create(
                session_id=self.session_id,
                provider_name=candidate_provider_name,
                model=candidate_model,
                permission_mode=candidate_permission,
                running_mode=candidate_running,
            )
        self.provider_name = candidate_provider_name
        # Keep a detached, fully validated snapshot on the bridge.  A caller
        # may mutate the PATCH dictionary after this method returns.
        self.model_config = dict(updated.model) if updated is not None else dict(candidate_model)
        self.model = str(candidate_model.get("current_model") or self.model)
        self.permission_mode = candidate_permission
        self.running_mode = candidate_running
        if self.runtime is not None:
            # PATCH requests carry partial fields.  Keep a merged pending
            # snapshot so two quick updates (for example reasoning followed by
            # permission) cannot cause the first update to disappear before
            # the next model/tool boundary consumes it.
            pending = dict(self.runtime.services.pending_runtime_config or {})
            pending_model = pending.get("model")
            incoming_model = config.get("model")
            if isinstance(pending_model, Mapping) or isinstance(incoming_model, Mapping):
                pending["model"] = {
                    **(dict(pending_model) if isinstance(pending_model, Mapping) else {}),
                    **(dict(incoming_model) if isinstance(incoming_model, Mapping) else {}),
                }
            pending.update({key: value for key, value in config.items() if key != "model"})
            self.runtime.services.pending_runtime_config = pending
        return updated

    def start(self) -> RuntimeState:
        if self.started:
            if self.last_node is None:
                raise RuntimeError("Node bridge has no starting node.")
            return self.last_node
        if self.source_node_id:
            self.parent = self.store.get_node(self.session_id, self.source_node_id)
            if self.parent is None:
                raise ValueError("source_node_id does not belong to the active session.")
            if self.store.list_children(self.parent.session_id, self.parent.id) and not self.allow_branch:
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
        # A continuation request may carry the dynamic leaf id while the
        # durable database still contains only its failed placeholder.  The
        # placeholder is intentionally used as the parent reference; its
        # contents are replaced by the writer's dynamic copy at the next
        # provider boundary and are never sent as an empty message.
        if (
            self.parent is not None
            and self.store.list_children(self.parent.session_id, self.parent.id)
            and not self.allow_branch
        ):
            raise ValueError("The continuation parent must be a leaf node.")
        if self.parent is None and self.source_node_id is None:
            # Configuration is part of the user node now; no synthetic change
            # nodes are written into the history.
            self.parent = None
        if self.parent is not None and not self.prompt:
            # ``/resume`` has no new user text.  Continue directly from the
            # paused/failed leaf instead of persisting an empty user message.
            # Resume configuration is represented by the next assistant node;
            # avoid mutating a sealed historical parent.
            self.last_node = self.parent
            self.started = True
            self._ensure_assistant()
            return self.last_node
        user_node = self.writer.create(
            session_id=self.session_id,
            parent=self.parent,
            data=message_payload(
                "user",
                self.prompt,
                source="user",
                **({"references": self.references} if self.references else {}),
            ),
            user=self.user,
            provider_name=self.provider_name,
            model=self.model_config,
            permission_mode=self.permission_mode,
            running_mode=self.running_mode,
            cwd=self.cwd,
        )
        self.last_node = self.writer.delete(user_node.session_id, user_node.id)
        self.started = True
        self.batch_call_ids = set()
        # Create the assistant sidecar before the first provider boundary so
        # runtime-config updates always target a dynamic leaf.  Its durable
        # placeholder is filtered from model context until it has content.
        self._ensure_assistant()
        return self.last_node

    def _ensure_assistant(self) -> RuntimeState:
        if self.assistant is None:
            self.assistant = self.writer.create(
                session_id=self.session_id,
                parent=self.last_node,
                data=message_payload("assistant", [], **({"run_id": self.run_id} if self.run_id else {})),
                user=self.user,
                provider_name=self.provider_name,
                model=self.model_config,
                permission_mode=self.permission_mode,
                running_mode=self.running_mode,
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

    def _persist_steering(self, data: Mapping[str, Any]) -> None:
        """Persist one steering input as a first-class user message node.

        Steering is a genuine user turn: it must appear in the canonical node
        projection in conversation order, not as a control block inside the
        previous assistant message.  An empty assistant placeholder that has
        not yet received any blocks is discarded instead of being sealed, so
        it cannot produce an empty assistant node ahead of the user message.
        """

        if self.assistant is not None:
            # An assistant with real text or tool results is preserved as a
            # node.  A placeholder that only carries unexecuted tool calls is
            # discarded like the in-memory message: the steering input
            # supersedes it, and sealing it would project an empty assistant
            # ahead of the user message.
            if any(block.get("type") in {"text", "tool_result"} for block in self.assistant_blocks):
                self._seal_assistant("success")
            else:
                self.assistant = None
        content = str(data.get("content") or "")
        node = self.writer.create(
            session_id=self.session_id,
            parent=self.last_node,
            data=message_payload(
                "user",
                content,
                source="steering",
                **({"run_id": self.run_id} if self.run_id else {}),
            ),
            provider_name=self.provider_name,
            model=self.model_config,
            permission_mode=self.permission_mode,
            running_mode=self.running_mode,
            cwd=self.cwd,
        )
        self.last_node = self.writer.delete(node.session_id, node.id, status="success")

    def _seal_assistant(self, status: NodeStatus = "success") -> None:
        if self.assistant is not None:
            self.last_node = self.writer.delete(self.session_id, self.assistant.id, status=status)
            self.assistant = None

    def _tool_call_in_context(self, call_id: str) -> bool:
        """Return whether a tool call was already declared on this path."""

        return bool(call_id and call_id in self.batch_call_ids)

    def _persist_tool_result(
        self,
        message: str,
        data: Mapping[str, Any],
        *,
        status: Literal["succeeded", "failed"],
        emit: bool = True,
    ) -> None:
        """Persist one tool result without creating a duplicate assistant."""

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
            "status": status,
            "replay_safe": bool(replay_safe),
        }
        if tool_name:
            result_block["tool"] = tool_name
        if data.get("side_effect") is not None:
            result_block["side_effect"] = bool(data["side_effect"])
        if isinstance(data.get("retryable"), bool):
            result_block["retryable"] = data["retryable"]
        elif data.get("failure_code") in {"user_denied", "user_denied_batch"}:
            result_block["retryable"] = False
        if isinstance(data.get("failure_code"), str):
            result_block["failure_code"] = data["failure_code"]
        previous_emit = self.writer.emit
        if not emit:
            self.writer.emit = lambda _frame: None
        try:
            result = self.writer.create(
                session_id=self.session_id,
                parent=self.last_node,
                data=message_payload("tool_result", result_block, **({"run_id": self.run_id} if self.run_id else {})),
                provider_name=self.provider_name,
                model=self.model_config,
                permission_mode=self.permission_mode,
                running_mode=self.running_mode,
                cwd=self.cwd,
            )
            self.last_node = self.writer.delete(
                result.session_id,
                result.id,
                status="success" if status == "succeeded" else "failed",
            )
        finally:
            self.writer.emit = previous_emit

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
        self.batch_call_ids = set()
        self.response_text = ""
        self.run_id = ""
        self.abort_category = None
        self.abort_code = ""
        self.terminal_error = None
        self.started = False
        self.closed = False
        self.start()

    def begin_turn(
        self,
        session_id: str,
        prompt: str,
        *,
        running_mode: str | None = None,
    ) -> None:
        """Start a fresh canonical turn after a workflow handoff.

        A plan review and its Agent implementation may share one SSE bridge.
        The normal ``response_start`` session-switch path is too late for a
        same-session handoff and would otherwise append the implementation
        answer to the plan assistant sidecar.  Seal the previous dynamic leaf
        first, reset the per-turn projection, and create a new user/assistant
        pair before the next execution boundary.
        """

        if self.closed:
            raise RuntimeError("Cannot begin a turn on a closed node bridge.")
        if running_mode is not None:
            if running_mode not in {"agent", "plan"}:
                raise RuntimeStateValidationError("running_mode must be agent or plan.")
            self.running_mode = running_mode
        if self.assistant is not None:
            self._seal_assistant("success")
        emit = self.writer.emit
        same_session = session_id == self.session_id
        if not same_session:
            self.writer = NodeWriter(self.store, emit=emit)
            self.parent = None
        else:
            # ``_seal_assistant`` leaves ``last_node`` as the new durable
            # parent, which is exactly what a same-session continuation needs.
            self.parent = self.last_node
        self.session_id = session_id
        self.prompt = prompt
        self.source_node_id = None
        self.assistant = None
        self.last_node = self.parent if same_session else None
        self.assistant_blocks = []
        self.batch_call_ids = set()
        self.response_text = ""
        self.run_id = ""
        self.abort_category = None
        self.abort_code = ""
        self.terminal_error = None
        self.persistence_failed = False
        self.started = False
        self.closed = False
        self.start()

    def handle(self, event: Any) -> None:
        if self.closed or not self.started:
            return
        kind = getattr(event, "kind", "")
        message = str(getattr(event, "message", "") or "")
        data = getattr(event, "data", {})
        if not isinstance(data, Mapping):
            data = {}
        if kind in _HIDDEN_RECOVERABLE_EVENTS:
            # Recoverable diagnostics stay out of user-facing nodes, but a
            # terminal error arriving immediately afterwards still needs the
            # most specific category for its visible terminal explanation.
            if kind == "tool_failed":
                if data.get("failure_code") not in {"user_denied", "user_denied_batch"}:
                    self._remember_abort("tool", code="tool_failed")
                self._persist_tool_result(message, data, status="failed", emit=False)
            return
        event_session_id = data.get("session_id")
        if isinstance(event_session_id, str) and event_session_id and event_session_id != self.session_id:
            self._switch_session(event_session_id, str(data.get("task") or self.prompt))
        if isinstance(data.get("run_id"), str) and data["run_id"]:
            self.run_id = str(data["run_id"])
        # Runtime configuration events are intentionally top-level updates on
        # the active dynamic node, never synthetic data.type nodes.
        config = data.get("runtime_config") or data.get("config")
        if isinstance(config, Mapping):
            self.apply_runtime_config(config)
        # ``node_usage`` is the already reconciled five-field projection.  It
        # is authoritative for the dynamic node (including tiktoken fallback
        # totals when a provider omitted usage); raw ``usage`` remains useful
        # for legacy event consumers and is used as a fallback.
        usage = data.get("node_usage") if isinstance(data.get("node_usage"), Mapping) else data.get("usage")
        if isinstance(usage, Mapping):
            self._apply_usage(usage)
        if kind in {"model_request", "response_start", "thinking_start"}:
            if kind in {"model_request", "response_start"}:
                self.batch_call_ids = set()
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
                batch_call_ids: set[str] = set()
                if isinstance(raw.get("reasoning"), str) and raw["reasoning"]:
                    blocks.append({"type": "reasoning", "text": raw["reasoning"]})
                if isinstance(raw.get("content"), str) and raw["content"]:
                    blocks.append({"type": "text", "text": raw["content"]})
                for tool in raw.get("tool_messages", []) if isinstance(raw.get("tool_messages"), list) else []:
                    if isinstance(tool, Mapping):
                        call_id = str(tool.get("call_id") or "call_unknown")
                        batch_call_ids.add(call_id)
                        blocks.append(
                            {
                                "type": "tool_call",
                                "call_id": call_id,
                                "name": str(tool.get("name") or "unknown"),
                                "arguments": dict(tool.get("arguments") or {}),
                                "replay_safe": bool(tool.get("replay_safe", tool.get("retryable", True) is not False)),
                            }
                        )
                self._ensure_assistant()
                self.assistant_blocks = blocks
                self.batch_call_ids = batch_call_ids
                self.response_text = str(raw.get("content") or "")
                self._update_assistant()
                self._apply_usage(raw.get("node_usage") or raw.get("usage"))
        elif kind == "tool_call":
            tool_name = str(data.get("tool") or data.get("name") or message or "unknown")
            call_id = str(data.get("call_id") or "call_unknown")
            # The assistant_message event can already contain the entire
            # batch. After the first result seals that assistant, subsequent
            # per-tool events must not create a duplicate assistant node for
            # a call that is already present in the current batch.
            if self.assistant is None and self._tool_call_in_context(call_id):
                return
            if self.batch_call_ids and call_id not in self.batch_call_ids:
                if self.assistant is not None:
                    self._seal_assistant("success")
                self.batch_call_ids = set()
            self._ensure_assistant()
            replay_safe = data.get("replay_safe")
            if replay_safe is None:
                lowered = tool_name.lower()
                replay_safe = not any(token in lowered for token in ("bash", "shell", "command", "write", "mcp"))
            if not any(
                block.get("type") == "tool_call" and str(block.get("call_id") or "") == call_id
                for block in self.assistant_blocks
            ):
                self.assistant_blocks.append(
                    {
                        "type": "tool_call",
                        "call_id": call_id,
                        "name": tool_name,
                        "arguments": dict(data.get("arguments") or {}),
                        "replay_safe": bool(replay_safe),
                    }
                )
            self._update_assistant()
        elif kind == "tool_result":
            self._persist_tool_result(message, data, status="succeeded")
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
        elif kind == "steering_applied":
            self._persist_steering(data)
        elif kind in {
            "plan",
            "feedback_received",
            "handoff_created",
            "steering_received",
            "model_repair",
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
                start = RuntimeStateTree._path_index(path, old_compaction) or 0
                source_ids = [item.id for item in path[start:]]
            first_kept = path[max(0, len(path) - DEFAULT_COMPACTION_RETENTION)].id if path else None
            node = self.writer.create(
                session_id=self.session_id,
                parent=self.last_node,
                data=compaction_payload(summary, source_ids=[str(item) for item in source_ids]),
                provider_name=self.provider_name,
                model=self.model_config,
                permission_mode=self.permission_mode,
                running_mode=self.running_mode,
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
            fallback = terminal_error_text(self.terminal_error)
            # A caller-provided terminal answer (for example ``fail_run``'s
            # exact message) is authoritative for the node text; the generic
            # terminal template is only a fallback for empty answers and for
            # answers that already equal the template (e.g. cancel wording).
            if not final_answer or final_answer == fallback:
                final_answer = fallback
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
            try:
                self._seal_assistant(status)
            except Exception:
                # ``NodeWriter.delete`` performs the durable replacement in a
                # transaction.  If that transaction or its final persistence
                # hook fails, retain the original empty failed placeholder;
                # the stream still receives a deterministic terminal error.
                self.persistence_failed = True
                self.terminal_error = terminal_error_payload("failed", code="runtime_node_persistence_failed")
                self.closed = True
                return None
        self.closed = True
        return self.last_node

    def preserve_placeholder(self, *, code: str = "runtime_exception") -> RuntimeState | None:
        """Close a stream without replacing its failed persistence marker.

        This is used for an uncaught/unknown worker exception.  The dynamic
        sidecar is process-local and must not be promoted to history merely
        because the error handler ran after the exception.
        """

        self.terminal_error = terminal_error_payload("failed", code=code)
        self.closed = True
        if self.assistant is None:
            # ``last_node`` may be an already sealed user/tool node.  Returning
            # it would make the SSE layer report a successful terminal state
            # for an exception that has no active assistant placeholder.
            return None
        try:
            return self.store.get_node(self.assistant.session_id, self.assistant.id)
        except Exception:
            return None

    def finish_exception(self, error: BaseException) -> RuntimeState | None:
        """Finish an uncaught worker exception without losing its category."""

        category = self.abort_category or self._exception_category(error)
        if category is None:
            return self.preserve_placeholder(code=error.__class__.__name__ or "runtime_exception")
        return self.finish(
            "abort",
            str(error),
            category=category,
            code=self.abort_code or error.__class__.__name__,
        )


__all__ = ["RuntimeEventNodeBridge"]
