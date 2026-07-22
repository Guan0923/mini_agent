"""Full-screen Textual view with a scrollable transcript and fixed input row."""

from __future__ import annotations

import asyncio
import random
from collections.abc import Callable
from threading import Lock, get_ident
from time import monotonic
from traceback import format_exception, format_stack

from rich.console import RenderableType
from textual import events, messages, on
from textual._context import active_app
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.timer import Timer
from textual.widgets import Input, ListView, OptionList, Static, TextArea
from textual.widgets.option_list import Option
from textual.widgets.text_area import Selection

from mini_agent.runtime.core.events import RuntimeEvent

from .choice_prompt import ChoicePromptMixin
from .completion import CommandSuggestion, SlashCommandCompleter
from .diagnostic_mixin import TuiDiagnosticMixin
from .diagnostics import DiagnosticSink
from .history import HistoryScreen
from .inspection import SessionsScreen, TraceScreen
from .latex import LatexMarkdown
from .transcript import CompactProgress, MarkdownBody, TranscriptNode
from .transcript_rendering import TranscriptRenderingMixin
from .widgets import (
    ChoiceItem,
    ChoiceRow,
    ContextProgress,
    InlineChoiceList,
    TerminalInput,
    TranscriptTextArea,
)

_COPY_NOTICE_SECONDS = 1.5
_RUNNING_STATUS_MIN_SECONDS = 5.0
_RUNNING_STATUS_MAX_SECONDS = 10.0

RUNNING_STATUS_WORDS = (
    "🧠 THINKING",
    "🧪 BREWING",
    "🧭 EXPLORING",
    "🔌 CONNECTING",
    "🧵 WEAVING",
    "🔎 SCOUTING",
    "🧩 ASSEMBLING",
    "✨ POLISHING",
    "📝 DRAFTING",
    "📚 READING",
    "🗺️ MAPPING",
    "🛠️ BUILDING",
    "⚙️ TUNING",
    "📐 STRUCTURING",
    "🔬 ANALYZING",
    "💡 SPARKING",
    "🌱 GROWING",
    "🚀 LAUNCHING",
    "🛰️ SCANNING",
    "🧮 COMPUTING",
    "🧹 SORTING",
    "🧱 STACKING",
    "🎯 FOCUSING",
    "🛡️ VALIDATING",
    "🔐 CHECKING",
    "📦 PACKING",
    "🔄 REFINING",
    "🧵 THREADING",
    "🧠 REASONING",
    "🗣️ FORMULATING",
    "📡 SIGNALING",
    "🧰 TOOLING",
    "🌊 FLOWING",
    "🎨 SHAPING",
    "✅ VERIFYING",
    "🏁 FINISHING",
    "🏃 RUNNING",
)

__all__ = [
    "ChoiceItem",
    "ChoiceRow",
    "ContextProgress",
    "InlineChoiceList",
    "RUNNING_STATUS_WORDS",
    "TerminalInput",
    "TerminalView",
    "TranscriptTextArea",
]

class TerminalView(ChoicePromptMixin, TranscriptRenderingMixin, TuiDiagnosticMixin, App[None]):
    """Own the Textual widgets and expose the small interface used by TerminalApp."""

    CSS = """
    Screen { background: #101418; color: #d7dde5; }
    #transcript {
        height: 1fr;
        border: none;
        padding: 0 1;
        background: #0b1016;
        color: #d7dde5;
        overflow-y: scroll;
    }
    .transcript-node {
        width: 1fr;
        height: auto;
        background: #0b1016;
        padding: 0;
    }
    .transcript-node > Contents {
        padding: 0 0 0 2;
    }
    .transcript-role {
        margin-bottom: 1;
        padding: 0 1;
    }
    .transcript-role > CollapsibleTitle {
        display: none;
    }
    .transcript-role > Contents {
        padding: 0 0 0 1;
    }
    .transcript-user {
        background: #17233a;
        color: #e6efff;
        border-left: solid #4f8cff;
    }
    .transcript-user > Contents {
        background: #17233a;
    }
    .transcript-assistant {
        background: #132b27;
        color: #e4f7f1;
        border-left: solid #35c6a3;
    }
    .transcript-assistant > Contents {
        background: #132b27;
    }
    .transcript-assistant .transcript-node {
        background: #132b27;
    }
    .transcript-assistant .transcript-node > Contents {
        background: #132b27;
    }
    .transcript-status {
        padding-left: 2;
        color: #9fc3e8;
    }
    .compact-progress {
        padding: 0 1 1 2;
        color: #9fc3e8;
    }
    #separator { color: #5f6b76; height: 1; }
    #status-bar {
        height: 1;
        width: 100%;
        background: #263442;
    }
    #status { width: 1fr; min-width: 1; padding: 0 1; background: #263442; color: #9fc3e8; }
    #context-progress { width: 1fr; min-width: 1; height: 1; padding: 0 1; background: #263442; }
    #completion-menu { height: auto; max-height: 8; display: none; }
    #choice-header {
        height: auto;
        max-height: 4;
        display: none;
        padding: 0 1;
        color: #d7dde5;
        background: #171c21;
    }
    #review-details {
        height: auto;
        max-height: 12;
        display: none;
        padding: 0 1;
        background: #171c21;
        overflow-y: auto;
    }
    .choice-list {
        height: auto;
        max-height: 8;
        display: none;
        background: #171c21;
    }
    .choice-row {
        width: 1fr;
        height: auto;
        padding: 0 1;
        background: #171c21;
    }
    .choice-row.-selected-answer {
        color: #9fc3e8;
    }
    .choice-label {
        width: 1fr;
        height: auto;
    }
    .choice-editor {
        width: 1fr;
        height: 1;
        display: none;
        border: none;
        padding: 0;
        background: #171c21;
        color: white;
    }
    #input {
        width: 100%;
        height: 3;
        margin-bottom: 0;
        border: none;
        padding: 0;
        background: #171c21;
        color: white;
    }
    """

    BINDINGS = [
        Binding("pageup", "page_up", show=False, priority=True),
        Binding("pagedown", "page_down", show=False, priority=True),
        Binding("ctrl+end", "follow_latest", show=False, priority=True),
        Binding("ctrl+c", "control_c", show=False, priority=True),
        Binding("ctrl+d", "control_d", show=False, priority=True),
    ]

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop | None = None,
        *,
        completer: SlashCommandCompleter | None = None,
        transcript_limit: int = 200_000,
        transcript_node_limit: int = 250,
        status_random: random.Random | None = None,
        diagnostic_sink: DiagnosticSink | None = None,
        log_full_messages: bool = True,
    ) -> None:
        super().__init__()
        self._owner_loop = loop
        self._owner_ready = False
        self._pending_owner_callbacks: list[Callable[[], None]] = []
        self._diagnostic_sink = diagnostic_sink
        self._log_full_messages = log_full_messages
        self._diagnostic_lock = Lock()
        self._last_runtime_event: dict[str, object] = {}
        self._stream_diagnostics: dict[tuple[str, str], tuple[int, int, float]] = {}
        self._completer = completer or SlashCommandCompleter()
        self._suggestions: list[CommandSuggestion] = []
        self._init_transcript_state(transcript_limit, transcript_node_limit)
        self._compact_node: TranscriptNode | None = None
        self._compact_progress: CompactProgress | None = None
        self._writes_closed = False
        self._follow_tail = True
        self._paused_scroll_y = 0.0
        self._status = "AGENT | IDLE"
        self._interrupt_enabled = False
        self._copy_notice_timer: Timer | None = None
        self._init_choice_prompt_state()
        self._status_random = status_random or random.Random()
        self._status_timer: Timer | None = None
        self._running_status: str | None = None
        self.submissions: asyncio.Queue[str | None] = asyncio.Queue()
        self.interrupts: asyncio.Queue[None] = asyncio.Queue()
        self.status_line = Static(id="status")
        self.context_progress = ContextProgress()
        self.question_header = Static(id="choice-header")
        self.review_details = LatexMarkdown(id="review-details")
        self.completion_menu = OptionList(id="completion-menu")
        self.input = TerminalInput(
            soft_wrap=True,
            show_line_numbers=False,
            id="input",
        )

    def compose(self) -> ComposeResult:
        yield self.transcript
        yield self.question_header
        yield self.review_details
        yield self.completion_menu
        yield self.input
        yield Horizontal(self.status_line, self.context_progress, id="status-bar")

    def on_mount(self) -> None:
        self._owner_loop = asyncio.get_running_loop()
        self._owner_ready = True
        if self._pending_owner_callbacks:
            self.call_after_refresh(self._flush_pending_owner_callbacks)
        self._diagnose("view_mounted", self.diagnostic_snapshot())
        if self._is_running_status(self._status):
            self._choose_running_status()
        self._render_status()
        self.input.focus()
        if self._is_running_status(self._status):
            self._schedule_running_status()
        if self._pending_chunks:
            self._schedule_flush()

    async def _process_messages_loop(self) -> None:
        """Record CancelledError origins before Textual silently ends the app."""

        try:
            await super()._process_messages_loop()
        except asyncio.CancelledError as error:
            task = asyncio.current_task()
            self._diagnose(
                "message_loop_cancelled",
                {
                    "task_cancelling": task.cancelling() if task is not None else None,
                    "traceback": "".join(format_exception(error)),
                    **self.diagnostic_snapshot(),
                },
                error,
            )
            raise

    def _flush_pending_owner_callbacks(self) -> None:
        callbacks = self._pending_owner_callbacks
        self._pending_owner_callbacks = []
        for callback in callbacks:
            callback()

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
    ) -> None:
        self._run_on_owner(
            lambda: self.context_progress.set_usage(estimated_tokens, context_size, threshold)
        )

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
        self._show_copy_notice(len(selected))
        self.input.focus()
        return True

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

    def action_control_c(self) -> None:
        self.submissions.put_nowait("/quit")

    def action_control_d(self) -> None:
        if self._choice_kind is not None:
            return
        if self.input.value:
            cursor = self.input.cursor_position
            self.input.value = self.input.value[:cursor] + self.input.value[cursor + 1 :]
            self.input.cursor_position = cursor
        else:
            self.submissions.put_nowait(None)

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        if event.text_area is not self.input:
            return
        self._resize_input()
        self._suggestions = self._completer.suggestions(self.input.value, self.input.cursor_position)
        self.completion_menu.clear_options()
        self.completion_menu.add_options(
            Option(f"{item.value} — {item.description}", id=str(index))
            for index, item in enumerate(self._suggestions)
        )
        self.completion_menu.display = bool(self._suggestions)
        if self._suggestions:
            self.completion_menu.highlighted = 0

    @on(TerminalInput.Submitted)
    def on_input_submitted(self, event: TerminalInput.Submitted) -> None:
        if event.input is not self.input:
            return
        if self.completion_menu.display and self._suggestions:
            self._accept_completion()
            return
        value = event.value
        self.input.value = ""
        self.submissions.put_nowait(value)

    def _resize_input(self) -> None:
        self.input.styles.height = max(3, min(self.input.wrapped_document.height, 4))

    def on_key(self, event: events.Key) -> None:
        editing = self._editing_row()
        if editing is not None and self.focused is editing.editor:
            if event.key != "escape":
                return
            editing.end_edit()
            self._active_choice_list().focus()
            event.prevent_default()
            event.stop()
            return

        focused = self.focused
        if isinstance(focused, InlineChoiceList) and focused in self._choice_lists:
            if event.key == "left" and self.questionnaire_active:
                self._move_question(-1)
            elif event.key == "right" and self.questionnaire_active:
                self._move_question(1)
            elif event.key == "tab":
                row = focused.highlighted_row
                if row is not None and row.choice.custom:
                    row.begin_edit(self._existing_custom_answer(focused))
            elif event.key == "escape" and self._interrupt_enabled:
                self._request_interrupt()
            else:
                return
            event.prevent_default()
            event.stop()
            return

        if event.key == "escape" and self._interrupt_enabled:
            self._request_interrupt()
            event.prevent_default()
            event.stop()
            return
        if not self.completion_menu.display or not self._suggestions or self.focused is not self.input:
            return
        if event.key in {"down", "up"}:
            current = self.completion_menu.highlighted or 0
            step = 1 if event.key == "down" else -1
            self.completion_menu.highlighted = (current + step) % len(self._suggestions)
        elif event.key in {"tab"}:
            self._accept_completion()
        elif event.key == "escape":
            self._hide_completions()
        else:
            return
        event.prevent_default()
        event.stop()

    @on(ListView.Selected)
    def on_choice_selected(self, event: ListView.Selected) -> None:
        self._handle_choice_selected(event)

    @on(Input.Submitted)
    def on_choice_input_submitted(self, event: Input.Submitted) -> None:
        self._handle_choice_input_submitted(event)

    def _request_interrupt(self) -> None:
        if self.interrupts.empty():
            self.interrupts.put_nowait(None)

    def _accept_completion(self) -> None:
        index = self.completion_menu.highlighted or 0
        suggestion = self._suggestions[index]
        end = self.input.cursor_position
        self.input.value = f"{self.input.value[:suggestion.start_position]}{suggestion.value}{self.input.value[end:]}"
        self.input.cursor_position = suggestion.start_position + len(suggestion.value)
        self._hide_completions()
        self.input.focus()

    def _hide_completions(self) -> None:
        self.completion_menu.display = False
        self._suggestions = []

    def _resume_follow_if_at_end(self) -> None:
        if self.transcript.scroll_y >= self.transcript.max_scroll_y:
            self._follow_tail = True
            self._refresh_status()
        else:
            self._remember_paused_scroll()

    def _show_copy_notice(self, character_count: int) -> None:
        self._invalidate_copy_notice()
        self.status_line.update(f" COPIED — {character_count} characters")
        timer: Timer | None = None

        def restore() -> None:
            self._restore_status_after_copy(timer)

        timer = self.set_timer(_COPY_NOTICE_SECONDS, restore)
        self._copy_notice_timer = timer

    def _invalidate_copy_notice(self) -> None:
        if self._copy_notice_timer is not None:
            self._copy_notice_timer.stop()
            self._copy_notice_timer = None

    def _restore_status_after_copy(self, timer: Timer | None) -> None:
        if timer is not self._copy_notice_timer:
            return
        self._copy_notice_timer = None
        self._render_status()

    def _refresh_status(self) -> None:
        self._invalidate_copy_notice()
        self._render_status()

    @staticmethod
    def _is_running_status(status: str) -> bool:
        return any(part.strip() == "RUNNING" for part in status.split("|"))

    def _schedule_running_status(self) -> None:
        if self._writes_closed or not self.is_running or not self._is_running_status(self._status):
            return
        delay = self._status_random.uniform(
            _RUNNING_STATUS_MIN_SECONDS,
            _RUNNING_STATUS_MAX_SECONDS,
        )
        self._status_timer = self.set_timer(delay, self._rotate_running_status)

    def _rotate_running_status(self) -> None:
        self._status_timer = None
        if not self._is_running_status(self._status):
            self._running_status = None
            return
        self._choose_running_status()
        if self._copy_notice_timer is None:
            self._render_status()
        self._schedule_running_status()
    def _choose_running_status(self) -> None:
        choices = tuple(word for word in RUNNING_STATUS_WORDS if word != self._running_status)
        self._running_status = self._status_random.choice(choices)


    def _stop_running_status(self) -> None:
        if self._status_timer is not None:
            self._status_timer.stop()
            self._status_timer = None
        self._running_status = None

    def _render_status(self) -> None:
        suffix = " | PgUp/PgDn scroll" if not self._follow_tail else ""
        status = self._status
        if self._running_status is not None:
            status = status.replace(" | RUNNING", f" | {self._running_status}", 1)
        self.status_line.update(f" {status}{suffix}")

    def _run_on_owner(
        self,
        callback: Callable[[], None],
        *,
        diagnostic_name: str = "owner_callback",
        diagnostic_data: dict[str, object] | None = None,
    ) -> None:
        def guarded() -> None:
            try:
                callback()
            except Exception as error:
                self._diagnose(
                    "owner_callback_failed",
                    {
                        "callback": diagnostic_name,
                        **dict(diagnostic_data or {}),
                        **self.diagnostic_snapshot(),
                    },
                    error,
                )
                raise

        if not self._owner_ready:
            self._pending_owner_callbacks.append(guarded)
            return
        try:
            if active_app.get() is self and self._thread_id == get_ident():
                guarded()
                return
        except LookupError:
            pass
        self.call_later(guarded)
