"""RuntimeEventNodeBridge construction, binding, and Turn startup."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from threading import RLock
from typing import Any

from backend.domain.runtime_state import (
    NodeFrame,
    NodeWriter,
    RuntimeNodeStore,
    RuntimeRootState,
    RuntimeState,
    RuntimeStateTree,
    RuntimeStateValidationError,
    TerminalErrorCategory,
)

from .events import _EventProjectionMixin
from .finalization import _FinalizationMixin
from .items import _ItemProjectionMixin
from .lifecycle import _LifecycleMixin


class RuntimeEventNodeBridge(_EventProjectionMixin, _FinalizationMixin, _LifecycleMixin, _ItemProjectionMixin):
    """Keep the canonical Turn synchronized with an in-process AgentRuntime."""

    def __init__(
        self,
        store: RuntimeNodeStore,
        *,
        session_id: str,
        prompt: str,
        turn_id: str | None = None,
        compaction_turn_id: str | None = None,
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
        delivery_id: str | None = None,
        emit: Callable[[NodeFrame], None],
    ) -> None:
        self.store = store
        self.session_id = session_id
        self.thread_id = thread_id or session_id
        self.turn_id = turn_id
        self.compaction_turn_id = compaction_turn_id
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
        self.delivery_id = delivery_id or ""
        self.writer = NodeWriter(store, emit=emit)
        self.parent: RuntimeState | RuntimeRootState | None = None
        self.assistant: RuntimeState | None = None
        self.last_node: RuntimeState | None = None
        self.assistant_blocks: list[dict[str, Any]] = []
        self.assistant_message_idx: int | None = None
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
        self._runtime_config_lock = RLock()

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

    def _latest_parent(self) -> RuntimeState | RuntimeRootState:
        nodes = [node for node in self.store.load_nodes(self.session_id) if node.thread_id == self.thread_id]
        turns = [node for node in nodes if isinstance(node, RuntimeState)]
        if turns:
            parent_keys = {(node.parent_session_id, node.parent_id) for node in turns if node.parent_id}
            leaves = [node for node in turns if node.key not in parent_keys]
            return max(leaves or turns, key=lambda node: (node.timestamp, node.id))
        if self.thread_id != self.session_id:
            raise RuntimeStateValidationError("A fork Thread must begin with a copied Turn.")
        return self.store.ensure_root_node(self.session_id)

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
            if isinstance(source, RuntimeRootState):
                raise ValueError("A root Turn is only an ancestry anchor.")
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
                selected = source.data[source.current_data_idx]
                self.assistant_message_idx = len(selected) - 1 if selected[-1]["role"] == "assistant" else None
                self.assistant_blocks = source.assistant_items if self.assistant_message_idx is not None else []
                if self.assistant_blocks and self.assistant_blocks[0].get("type") == "compaction":
                    self.protected_item_count = 1 + int(self.assistant_blocks[0].get("kept_item_count") or 0)
                self.started = True
                return source
            self.parent = source
            if self.thread_id == self.session_id:
                self.thread_id = source.thread_id
        else:
            self.parent = self._latest_parent()
        user_item: dict[str, Any] = {"type": "text", "text": self.prompt, "status": "success"}
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
            data=None,
        )
        if self.delivery_id:
            node.data[0][0]["delivery_id"] = self.delivery_id
            node = RuntimeState.from_dict(node.to_dict())
        node = self.writer.create(node)
        self.assistant = node
        self.last_node = node
        self.turn_id = node.id
        self.assistant_message_idx = 1
        self.started = True
        return node

    def _ensure_assistant_message(self) -> None:
        if self.assistant is None:
            self.start()
        assert self.assistant is not None
        current = self.writer.current(self.assistant.session_id, self.assistant.id)
        messages = current.data[current.current_data_idx]
        if messages[-1]["role"] == "user":
            current = self.writer.append_message(
                current,
                {"role": "assistant", "content": []},
                persist=True,
            )
            messages = current.data[current.current_data_idx]
            self.assistant_blocks = []
        self.assistant = current
        self.last_node = current
        self.assistant_message_idx = len(messages) - 1

    def _append_steering_message(self, data: Mapping[str, Any]) -> None:
        content = str(data.get("content") or "").strip()
        references = data.get("references")
        if not content and not references:
            return
        self._finish_stream_item()
        if self.assistant is None:
            self.start()
        assert self.assistant is not None
        item: dict[str, Any] = {"type": "text", "text": content, "status": "success"}
        if isinstance(references, list) and references:
            item["references"] = self._json_value(references)
        message: dict[str, Any] = {"role": "user", "content": [item]}
        delivery_id = str(data.get("delivery_id") or "")
        if delivery_id:
            selected = self.assistant.data[self.assistant.current_data_idx]
            if any(value.get("role") == "user" and value.get("delivery_id") == delivery_id for value in selected):
                return
            message["delivery_id"] = delivery_id
        self.assistant = self.writer.append_message(self.assistant, message, persist=True)
        self.last_node = self.assistant
        self.assistant_blocks = []
        self.assistant_message_idx = None

    def start_for_compaction(self) -> RuntimeState | None:
        if not self.started:
            self.parent = (
                self.store.get_node(self.session_id, self.source_node_id)
                if self.source_node_id
                else self._latest_parent()
            )
            if self.parent is None:
                raise ValueError("Unknown source Turn.")
            if isinstance(self.parent, RuntimeRootState):
                raise ValueError("A root Turn is only an ancestry anchor.")
            if self.parent.status != "success":
                raise ValueError("Only a successful Turn can be compacted.")
            self.thread_id = self.parent.thread_id
            self.last_node = self.parent
            self.assistant = None
            self.started = True
        return self.last_node
