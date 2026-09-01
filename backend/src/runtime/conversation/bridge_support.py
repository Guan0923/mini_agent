"""Conversation-level Turn bridge creation, binding, and compaction."""

from __future__ import annotations

from collections.abc import Mapping

from backend.domain import RunMode, RuntimeStateNode

from ..core.context import text_messages
from ..core.contracts import EventHandler
from ..core.events import RuntimeEvent
from ..node_bridge import RuntimeEventNodeBridge


class ConversationNodeBridgeMixin:
    def attach_runtime_node_bridge(
        self,
        bridge: RuntimeEventNodeBridge,
        *,
        events_external: bool = True,
    ) -> None:
        """Attach a caller-owned bridge for the next execution.

        The Web SSE layer needs to register the bridge before the worker
        starts, while the local service creates one lazily.  Marking event
        ownership prevents the Web sink from receiving the same event twice.
        """

        self.runtime_node_bridge = bridge
        self._node_bridge_events_external = events_external

    def compact_context(
        self,
        *,
        source_node_id: str | None = None,
        compact_turn_id: str | None = None,
    ):
        """Compact an idle conversation through the canonical message-tree bridge."""

        if self.runtime is None:
            return super().compact_context()
        bridge = self.runtime_node_bridge
        if bridge is None or bridge.closed:
            bridge = self._node_bridge_for_runtime(
                "",
                source_node_id=source_node_id,
                compaction_turn_id=compact_turn_id,
            )
            self.runtime_node_bridge = bridge
            self._node_bridge_events_external = False
        previous_on_event = self.runtime.services.on_event
        if bridge is not None:
            bridge.bind_runtime(self.runtime)
            bridge.start_for_compaction()

            def sink(event):
                bridge.handle(event)
                if previous_on_event is not None:
                    previous_on_event(event)

            self.runtime.services.on_event = sink
        canonical_context = bool(self.runtime.model_nodes())
        try:
            result = super().compact_context()
            if result.compacted and bridge is not None and bridge.finish("success") is None:
                raise RuntimeError("Compaction Turn could not be finalized.")
        finally:
            self.runtime.services.on_event = previous_on_event
            if bridge is not None:
                bridge.closed = True
            self.runtime_node_bridge = None
            self._node_bridge_events_external = False
        if canonical_context and result.compacted:
            # Refresh the in-process planner state from the durable Turn tree.
            projected = self.runtime.model_messages()
            self.runtime.state.messages = projected
            if self.runtime.state.current_run is not None:
                self.runtime.state.current_run.history = self.runtime.state.messages
                self.runtime.state.current_run.turn_start_index = min(1, len(projected))
            self.runtime.save()
        self.conversation = text_messages(self.runtime.state.messages)
        return result

    def compact_turn(self, source_node_id: str, compact_turn_id: str) -> RuntimeStateNode:
        """Create one finalized Compaction Turn from an exact source Turn."""

        result = self.compact_context(
            source_node_id=source_node_id,
            compact_turn_id=compact_turn_id,
        )
        if not result.compacted or self.session_store is None or self.active_session is None:
            raise RuntimeError("Conversation context did not produce a Compaction Turn.")
        getter = getattr(self.session_store, "get_node", None)
        if not callable(getter):
            raise RuntimeError("The Turn store cannot load the completed Compaction Turn.")
        compacted = getter(self.active_session.session_id, compact_turn_id)
        if not isinstance(compacted, RuntimeStateNode) or compacted.status != "success":
            raise RuntimeError("The completed Compaction Turn is unavailable.")
        return compacted

    def generate_title(self, first_user_text: str) -> str:
        """Generate a title with the active conversation's completed runtime."""

        if self.runtime is None:
            raise RuntimeError("Conversation runtime is unavailable for title generation.")
        return self.runner.generate_title(self.runtime, first_user_text)

    def _node_bridge_for_runtime(
        self,
        prompt: str,
        references: list[Mapping[str, str]] | None = None,
        *,
        source_node_id: str | None = None,
        compaction_turn_id: str | None = None,
        adopt_existing: bool = False,
    ) -> RuntimeEventNodeBridge | None:
        """Create a local bridge from the latest durable node configuration."""

        if self.session_store is None or not callable(getattr(self.session_store, "create_node", None)):
            return None
        session = self.active_session
        if session is None or self.runtime is None:
            return None
        store = self.session_store
        # Prefer the latest durable leaf's top-level runtime settings.  This
        # preserves a provider/model/permission change across turns even when
        # the legacy RuntimeState checkpoint still has older compatibility
        # fields.  A provider client supplies defaults for an empty session.
        latest = None
        if source_node_id:
            getter = getattr(store, "get_node", None)
            latest = getter(session.session_id, source_node_id) if callable(getter) else None
            if latest is None:
                raise ValueError("Unknown source Turn.")
            if not isinstance(latest, RuntimeStateNode):
                raise ValueError("A root Turn is only an ancestry anchor.")
        loader = getattr(store, "load_nodes", None)
        if latest is None and callable(loader):
            nodes = [node for node in loader(session.session_id) if isinstance(node, RuntimeStateNode)]
            if nodes:
                parent_keys = {(node.parent_session_id, node.parent_id) for node in nodes if node.parent_id}
                leaves = [node for node in nodes if (node.session_id, node.id) not in parent_keys]
                if leaves:
                    latest = max(leaves, key=lambda node: (node.timestamp, node.id))

        client = getattr(getattr(self.runtime.services, "planner", None), "client", None)
        config = getattr(client, "config", None)
        provider_name = str(
            (latest.provider_name if latest is not None else "")
            or getattr(self.runtime.state, "provider_name", "")
            or getattr(config, "provider_name", None)
            or getattr(config, "provider", None)
            or "unknown"
        )
        model_config = dict(latest.model) if latest is not None else dict(self.runtime.state.model_snapshot or {})
        model_config.setdefault(
            "current_model", getattr(config, "model", None) or self.runtime.state.model or "unknown"
        )
        model_config.setdefault("context_length", getattr(config, "context_size", 128000))
        model_config.setdefault("output_length", getattr(config, "max_tokens", 8192))
        model_config.setdefault("reasoning_effort", "medium")
        model_config.setdefault("thinking", "enable")
        model_config.setdefault("temperature", getattr(config, "temperature", 0.0))
        permission_mode = latest.permission_mode if latest is not None else self.runtime.state.permission_mode
        running_mode = latest.running_mode if latest is not None else self.runtime.state.running_mode
        return RuntimeEventNodeBridge(
            store,
            session_id=session.session_id,
            thread_id=latest.thread_id if latest is not None else session.session_id,
            source_node_id=latest.id if source_node_id and latest is not None else None,
            compaction_turn_id=compaction_turn_id,
            adopt_existing=adopt_existing,
            prompt=prompt,
            user=str(getattr(self.runtime.state, "user", "") or ""),
            provider_name=provider_name,
            model=str(model_config.get("current_model") or "unknown"),
            model_config=model_config,
            permission_mode=permission_mode,
            running_mode=running_mode,
            cwd=str(getattr(self.runtime.state, "workspace_root", "") or ""),
            project_cwd=str(getattr(self.runtime.state, "project_cwd", "") or ""),
            references=references,
            emit=lambda _frame: None,
        )

    def _bind_resume_node_bridge(self, turn_id: str, on_event: EventHandler | None) -> RuntimeEventNodeBridge:
        """Adopt the interrupted Turn so recovery appends Items in place."""

        if self.runtime is None:
            raise RuntimeError("Conversation runtime is unavailable for resume.")
        bridge = self.runtime_node_bridge
        if bridge is None or bridge.closed:
            bridge = self._node_bridge_for_runtime(
                "",
                source_node_id=turn_id,
                adopt_existing=True,
            )
            if bridge is None:
                raise RuntimeError("The Turn store cannot resume the interrupted Turn.")
            self.runtime_node_bridge = bridge
            self._node_bridge_events_external = False
        bridge.bind_runtime(self.runtime)
        current = bridge.start()
        if current.id != turn_id:
            raise RuntimeError("The attached Turn bridge does not match the resumed Turn.")
        bridge._bind_existing_trace(current)
        run = self.runtime.run
        run.thread_id = current.thread_id
        run.turn_id = current.id
        run.data_idx = current.current_data_idx

        if self._node_bridge_events_external:
            # Web SSE already owns RuntimeEvent projection and frame
            # publication. Reusing its pre-started bridge is essential: a
            # second dynamic writer would diverge after decision_requested and
            # collide with the immutable Turn Trace coordinate.
            self.runtime.services.on_event = on_event
            return bridge

        def sink(event: RuntimeEvent) -> None:
            bridge.handle(event)
            if on_event is not None:
                on_event(event)

        self.runtime.services.on_event = sink
        return bridge

    def _finish_resume_node_bridge(self, state) -> None:
        """Finalize and release an embedding-owned resumed Turn bridge."""

        bridge = self.runtime_node_bridge
        if bridge is None or self._node_bridge_events_external or state.handoff is not None:
            return
        if state.status in {"completed", "success"}:
            bridge.finish("success", state.final_answer or "")
        elif state.status == "cancelled":
            bridge.finish("paused", state.final_answer or "", category="user")
        elif bridge.abort_category is not None:
            bridge.finish(
                "paused" if bridge.abort_category == "network" else "failed",
                state.final_answer or "",
                category=bridge.abort_category,
                code=bridge.abort_code,
            )
        else:
            bridge.finish("failed", state.final_answer or "", category="agent", code="runtime_failed")
        if bridge.closed:
            self.runtime_node_bridge = None
            self._node_bridge_events_external = False

    def _fail_resume_node_bridge(self, bridge: RuntimeEventNodeBridge, error: Exception) -> None:
        """Finalize only a locally owned resume bridge after an exception."""

        if bridge is self.runtime_node_bridge and not self._node_bridge_events_external:
            bridge.finish_exception(error)

    def _bind_node_bridge(
        self,
        prompt: str,
        on_event: EventHandler | None,
        references: list[Mapping[str, str]] | None = None,
        *,
        running_mode: RunMode | None = None,
    ) -> None:
        """Bind a bridge to the runtime and compose its local event sink."""

        bridge = self.runtime_node_bridge
        if bridge is None or bridge.closed:
            bridge = self._node_bridge_for_runtime(prompt, references)
            self.runtime_node_bridge = bridge
            self._node_bridge_events_external = False
        if bridge is None or self.runtime is None:
            return
        if running_mode in {"agent", "plan"}:
            bridge.running_mode = running_mode
        bridge.bind_runtime(self.runtime)
        if not bridge.started:
            bridge.start()
        current = bridge._current()
        if current is not None and self.runtime.state.current_run is not None:
            run = self.runtime.state.current_run
            run.thread_id = current.thread_id
            run.turn_id = current.id
            run.data_idx = current.current_data_idx
        if self._node_bridge_events_external:
            # The caller (Web SSE) already invokes bridge.handle from its
            # transport sink and owns frame publication/active registration.
            return
        previous = on_event

        def sink(event: RuntimeEvent) -> None:
            bridge.handle(event)
            if previous is not None:
                previous(event)

        self.runtime.services.on_event = sink
