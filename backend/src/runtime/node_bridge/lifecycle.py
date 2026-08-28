"""Multi-node lifecycle operations used by Plan handoffs and compaction."""

from __future__ import annotations

from backend.domain.runtime_state import NodeStatus, RuntimeState, RuntimeStateValidationError, terminal_error_payload


class _LifecycleMixin:
    def finalize_current(self, status: NodeStatus = "success") -> RuntimeState:
        """Finalize the active node while keeping this multi-node stream open."""

        if status == "running":
            raise ValueError("A finalized Turn cannot remain running.")
        self._finish_stream_item(status="success" if status == "success" else "failed")
        self._settle_running_items("success" if status == "success" else "failed")
        current = self.assistant or self.last_node
        if current is None:
            current = self.start()
        if current.status == "running":
            current = self.writer.finalize(current, status)
        self.assistant = current
        self.last_node = current
        return current

    def start_child(self, prompt: str, *, running_mode: str) -> RuntimeState:
        """Create the next child Turn in the same Session, Thread, and SSE stream."""

        if running_mode not in {"agent", "plan"}:
            raise RuntimeStateValidationError("running_mode must be agent or plan.")
        parent = self.finalize_current("success")
        self.prompt = prompt
        self.parent = parent
        self.running_mode = running_mode
        child = RuntimeState.create(
            session_id=parent.session_id,
            thread_id=parent.thread_id,
            parent=parent,
            user_content=[{"type": "text", "text": prompt, "status": "success"}],
            user=parent.user or self.user,
            provider_name=parent.provider_name or self.provider_name,
            model=parent.model,
            permission_mode=parent.permission_mode,
            running_mode=running_mode,
            cwd=parent.cwd or self.cwd,
        )
        child = self.writer.create(child)
        self.assistant = child
        self.last_node = child
        self.turn_id = child.id
        self.assistant_blocks = child.assistant_items
        self.protected_item_count = 0
        self._stream_item_index = None
        self._stream_item_type = None
        self._stream_text = ""
        self.produced_item = False
        self.abort_category = None
        self.abort_code = ""
        self.terminal_error = None
        self.closed = False
        return child

    def record_compaction_failure(self, message: str) -> RuntimeState:
        """Persist a safe failure on the successful Plan node without creating a child."""

        self._append_item(terminal_error_payload("agent", message, retryable=True))
        return self.finalize_current("success")
