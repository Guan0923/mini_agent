"""Terminal status, error preservation, and exception mapping."""

from __future__ import annotations

from backend.domain.runtime_state import NodeStatus, RuntimeState, TerminalErrorCategory, terminal_error_payload


class _FinalizationMixin:
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
        self._ensure_assistant_message()
        self._finish_stream_item(status="success" if status == "success" else "failed")
        if (
            final_answer
            and status == "success"
            and not any(item.get("type") == "text" for item in self.assistant_blocks)
        ):
            self._append_item({"type": "text", "text": final_answer, "status": "success"})
        self._settle_running_items("success" if status == "success" else "failed")
        if status == "failed" or (status == "paused" and category != "user"):
            retryable = status == "paused"
            self.terminal_error = terminal_error_payload(
                category or ("user" if retryable else "agent"),
                final_answer or "Execution did not complete.",
                retryable=retryable,
                code=code,
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
