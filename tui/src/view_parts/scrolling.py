"""Transcript writing, scrolling, copying, and application exit."""

from __future__ import annotations

from traceback import format_stack

from rich.console import RenderableType
from textual import messages
from textual.widgets.text_area import Selection

from ..screens.history import HistoryScreen
from ..screens.inspection import SessionsScreen, TraceScreen


class ViewScrollingMixin:
    def write(self, text: str, end: str = "\n") -> None:
        value = f"{text}{end}"
        if not value or self._writes_closed:
            return
        should_schedule = False
        with self._pending_lock:
            self._pending_chunks.append(value)
            if not self._flush_scheduled:
                self._flush_scheduled = True
                should_schedule = True
        if should_schedule:
            loop = self._owner_loop
            if loop is not None and loop.is_running():
                loop.call_soon_threadsafe(self._schedule_flush)

    def clear(self) -> None:
        self._run_on_owner(self._clear_now)

    def set_ui(self, *, status: str, interrupt_enabled: bool = False) -> None:
        def update() -> None:
            was_running = self._is_running_status(self._status)
            is_running = self._is_running_status(status)
            if is_running and (not was_running or status != self._status):
                self._stop_running_status()
            elif not is_running and (was_running or self._running_status is not None):
                self._stop_running_status()
            self._status = status
            self._interrupt_enabled = interrupt_enabled
            if is_running and self._running_status is None:
                self._choose_running_status()
            if is_running and self._status_timer is None:
                self._schedule_running_status()
            self._refresh_status()

        self._run_on_owner(update)

    def set_context_usage(
        self,
        estimated_tokens: int | None = None,
        context_size: int | None = None,
        threshold: float = 0.8,
        *,
        cumulative_tokens: int | None = None,
    ) -> None:
        self._run_on_owner(
            lambda: self.context_progress.set_usage(
                estimated_tokens,
                context_size,
                threshold,
                cumulative_tokens=cumulative_tokens,
            )
        )

    def clear_context_usage(self) -> None:
        self._run_on_owner(self.context_progress.clear_usage)

    def stop(self) -> None:
        if self._writes_closed:
            return
        self._writes_closed = True
        self._stop_compaction_progress()
        self._diagnose("view_stop_requested", self.diagnostic_snapshot())

        def close() -> None:
            self.flush_now()
            self._stop_running_status()
            if self.is_running:
                self.exit()

        self._run_on_owner(close)

    def flush_now(self) -> None:
        chunks = self._take_pending_chunks()
        if chunks:
            self._append_transcript("".join(chunks))

    def scroll_page_up(self) -> None:
        if isinstance(self.screen, (HistoryScreen, SessionsScreen, TraceScreen)):
            self.screen.action_page_up()
            return
        if self.review_details.display:
            self.review_details.scroll_page_up(animate=False)
            return
        self._follow_tail = False
        self.transcript.scroll_page_up(animate=False)
        self._remember_paused_scroll()
        self._refresh_status()

    def scroll_page_down(self) -> None:
        if isinstance(self.screen, (HistoryScreen, SessionsScreen, TraceScreen)):
            self.screen.action_page_down()
            return
        if self.review_details.display:
            self.review_details.scroll_page_down(animate=False)
            return
        self.transcript.scroll_page_down(animate=False)
        self.call_after_refresh(self._resume_follow_if_at_end)

    def follow_latest(self) -> None:
        self._follow_tail = True
        self._paused_scroll_y = 0.0
        self.call_after_refresh(self._settle_follow_latest)
        self._refresh_status()

    def _settle_follow_latest(self) -> None:
        if not self._follow_tail:
            return
        self.transcript.scroll_end(animate=False)

    def pause_following(self) -> None:
        self._follow_tail = False
        self._remember_paused_scroll()
        self._refresh_status()

    def _remember_paused_scroll(self) -> None:
        if not self._follow_tail:
            self._paused_scroll_y = self.transcript.scroll_y

    def copy_transcript_selection(self) -> bool:
        selected = self.screen.get_selected_text() or self.transcript.selected_text
        if not selected:
            self.input.focus()
            return False
        selection_end = self.transcript.selection.end
        self.copy_to_clipboard(selected)
        self.screen.clear_selection()
        self.transcript.selection = Selection.cursor(selection_end)
        self._copy_focus_suspended = True
        self._show_copy_notice(len(selected))
        return True

    def focus_input_if_no_selection(self) -> None:
        """Restore default input focus only after a later selection-free interaction."""

        if getattr(self, "_copy_focus_suspended", False):
            self._copy_focus_suspended = False
            self.input.focus()
            return
        if self.screen.get_selected_text() or self.transcript.selected_text:
            return
        self.input.focus()

    def blur_input_for_transcript_selection(self) -> None:
        """Prevent a transcript selection from receiving terminal paste input."""

        if self.screen.get_selected_text() or self.transcript.selected_text:
            self.set_focus(None)

    def action_page_up(self) -> None:
        self.scroll_page_up()

    def action_page_down(self) -> None:
        self.scroll_page_down()

    def action_follow_latest(self) -> None:
        self.follow_latest()

    async def action_quit(self) -> None:
        """Record Textual's built-in quit action before preserving its behavior."""

        self._diagnose(
            "quit_action",
            {"focused_widget": type(self.focused).__name__ if self.focused is not None else None},
        )
        await super().action_quit()

    async def _on_exit_app(self) -> None:
        """Record dispatch of Textual's ExitApp message."""

        self._diagnose("exit_app_message", {"stack": "".join(format_stack())})
        await super()._on_exit_app()

    async def _on_close_messages(self, message: messages.CloseMessages) -> None:
        """Record direct message-pump closure requests."""

        sender = getattr(message, "_sender", None)
        self._diagnose(
            "close_messages_message",
            {"sender_type": type(sender).__name__ if sender is not None else None, "stack": "".join(format_stack())},
        )
        await super()._on_close_messages(message)

    def exit(
        self,
        result: None = None,
        return_code: int = 0,
        message: RenderableType | None = None,
    ) -> None:
        """Record every direct App.exit call before closing the message loop."""

        self._diagnose(
            "view_exit_called",
            {
                "return_code": return_code,
                "message_present": message is not None,
                "stack": "".join(format_stack()),
            },
        )
        super().exit(result, return_code, message)
