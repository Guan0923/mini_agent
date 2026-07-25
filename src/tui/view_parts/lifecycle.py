"""Conversation lifecycle and runtime-event routing."""

from __future__ import annotations

from time import monotonic

from backend.runtime.core.events import RuntimeEvent

from ..rendering.transcript import CompactProgress, MarkdownBody
from ..screens.history import HistoryScreen
from ..screens.inspection import SessionsScreen, TraceScreen


class ViewLifecycleMixin:
    @property
    def follow_tail(self) -> bool:
        return self._follow_tail

    @property
    def transcript_text(self) -> str:
        return self.transcript.text

    def begin_conversation(self, user_input: str) -> None:
        """Append a USER / ASSISTANT pair before a run is assigned its run id."""

        def begin() -> None:
            user = self._new_top_level("USER", completed=True)
            user_body = MarkdownBody(user_input)
            self._register_body(user_body, user)
            user.add_node(user_body)
            assistant = self._new_top_level("ASSISTANT")
            group = (user, assistant)
            self._top_level_groups[user] = group
            self._top_level_groups[assistant] = group
            self._pending_assistants.append(assistant)
            self._scroll_after_transcript_change()

        self._run_on_owner(begin)

    def queue_message(self, user_input: str) -> None:
        """Show a message waiting for the active run without creating a conversation turn."""

        def queue() -> None:
            self.queued_messages.set_messages([*self.queued_messages.messages, user_input])

        self._run_on_owner(queue)

    def clear_queued_messages(self) -> None:
        """Remove queue-only UI after its messages have started as a real run."""

        self._run_on_owner(lambda: self.queued_messages.set_messages([]))

    def begin_compaction(self) -> None:
        """Show an animated compaction row in the main transcript."""

        def begin() -> None:
            self._stop_compaction_progress()
            node = self._new_top_level("COMPACT")
            progress = CompactProgress()
            node.add_node(progress)
            self._compact_node = node
            self._compact_progress = progress
            self._scroll_after_transcript_change()

        self._run_on_owner(begin)

    def finish_compaction(
        self,
        *,
        compacted: bool,
        previous_messages: int,
        remaining_messages: int,
    ) -> None:
        """Replace the active animation with its final result."""

        def finish() -> None:
            progress = self._compact_progress
            if progress is None:
                return
            if compacted:
                progress.complete(previous_messages, remaining_messages)
            else:
                progress.no_op()
            if self._compact_node is not None:
                self._completed_top_levels.add(self._compact_node)
            self._compact_progress = None
            self._compact_node = None
            self._scroll_after_transcript_change()

        self._run_on_owner(finish)

    def fail_compaction(self, message: str) -> None:
        """Replace the active animation with a failure result."""

        def fail() -> None:
            if self._compact_progress is not None:
                self._compact_progress.fail(message)
            if self._compact_node is not None:
                self._completed_top_levels.add(self._compact_node)
            self._compact_progress = None
            self._compact_node = None
            self._scroll_after_transcript_change()

        self._run_on_owner(fail)

    def _stop_compaction_progress(self) -> None:
        if self._compact_progress is not None:
            self._compact_progress.stop()

    def write_system(self, text: str, end: str = "\n") -> None:
        """Keep non-conversation output in diagnostics without rendering it."""
        value = f"{text}{end}"
        if not value or self._writes_closed:
            return
        data: dict[str, object] = {
            "hidden": True,
            "message_chars": len(text),
            "end": end,
        }
        if self._log_full_messages:
            data["message"] = text
        self._diagnose("system_output_hidden", data)

    def show_history(self, session_label: str, messages: list[dict[str, str]]) -> None:
        """Push a read-only history screen without replacing the live transcript."""

        self._run_on_owner(lambda: self.push_screen(HistoryScreen(session_label, messages)))

    def show_sessions(self, sessions: list[str]) -> None:
        """Push a read-only saved-sessions screen."""

        self._run_on_owner(lambda: self.push_screen(SessionsScreen(sessions)))

    def show_trace(self, run_label: str, trace: str) -> None:
        """Push a read-only trace screen without replacing the live transcript."""

        self._run_on_owner(lambda: self.push_screen(TraceScreen(run_label, trace)))

    def load_history(self, messages: list[dict[str, str]]) -> None:
        """Replace the rendered transcript with persisted user and assistant messages."""

        def load() -> None:
            self._reset_transcript_state()
            for message in messages:
                role = message.get("role", "system").lower()
                if role not in {"user", "assistant"}:
                    continue
                node = self._new_top_level(role.upper(), completed=True)
                body = MarkdownBody(message.get("content", ""))
                self._register_body(body, node)
                node.add_node(body)
            self._scroll_after_transcript_change()

        self._run_on_owner(load)

    def handle_runtime_event(self, event: RuntimeEvent) -> None:
        """Route runtime events into their run's ordered ASSISTANT branch."""
        metadata = self._runtime_event_metadata(event)
        is_stream_delta = event.kind in {"response_delta", "thinking_delta"}
        self._update_stream_diagnostics(event)
        if not is_stream_delta:
            self._diagnose("runtime_event_queued", metadata)

        def handle() -> None:
            started = monotonic()
            if not is_stream_delta:
                self._diagnose("runtime_event_started", {**metadata, **self.diagnostic_snapshot()})
            self._handle_runtime_event_now(event)
            if not is_stream_delta:
                self._diagnose(
                    "runtime_event_finished",
                    {
                        **metadata,
                        "elapsed_ms": round((monotonic() - started) * 1_000, 3),
                        **self.diagnostic_snapshot(),
                    },
                )

        self._run_on_owner(handle, diagnostic_name=f"runtime_event:{event.kind}", diagnostic_data=metadata)
