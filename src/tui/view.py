"""Full-screen Textual view with a scrollable transcript and fixed input row."""

from __future__ import annotations

import asyncio
import random
from collections.abc import Callable
from threading import Lock
from traceback import format_exception

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.timer import Timer
from textual.widgets import OptionList, Static

from .components.choice_prompt import ChoicePromptMixin
from .components.completion import CommandSuggestion, SlashCommandCompleter
from .latex import LatexMarkdown
from .rendering.transcript import TranscriptNode
from .rendering.transcript_content import CompactProgress
from .rendering.transcript_rendering import TranscriptRenderingMixin
from .screens.diagnostic_mixin import TuiDiagnosticMixin
from .screens.diagnostics import DiagnosticSink
from .styles import TERMINAL_CSS
from .view_parts.input import ViewInputMixin
from .view_parts.lifecycle import ViewLifecycleMixin
from .view_parts.scrolling import ViewScrollingMixin
from .view_parts.status import RUNNING_STATUS_WORDS, ViewStatusMixin
from .widgets import (
    ChoiceItem,
    ChoiceRow,
    ContextProgress,
    InlineChoiceList,
    InputFrame,
    QueuedMessages,
    TerminalInput,
    TranscriptTextArea,
)

__all__ = [
    "ChoiceItem",
    "ChoiceRow",
    "ContextProgress",
    "InlineChoiceList",
    "InputFrame",
    "QueuedMessages",
    "RUNNING_STATUS_WORDS",
    "TerminalInput",
    "TerminalView",
    "TranscriptTextArea",
]


class TerminalView(
    ViewLifecycleMixin,
    ViewScrollingMixin,
    ViewInputMixin,
    ViewStatusMixin,
    ChoicePromptMixin,
    TranscriptRenderingMixin,
    TuiDiagnosticMixin,
    App[None],
):
    """Own the Textual widgets and expose the small interface used by TerminalApp."""

    CSS = TERMINAL_CSS

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
        detail_level: str = "medium",
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
        self._init_transcript_state(transcript_limit, transcript_node_limit, detail_level)
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
        # Details may be long, but review title and actions must remain fixed.
        self.choice_panel = Vertical(self.question_header, self.review_details, id="choice-panel")
        self.queued_messages = QueuedMessages()
        self.completion_menu = OptionList(id="completion-menu")
        self.input = TerminalInput(
            soft_wrap=False,
            show_line_numbers=False,
            id="input",
        )
        self.input_frame = InputFrame(self.input)

    def compose(self) -> ComposeResult:
        yield self.transcript
        yield self.queued_messages
        yield self.choice_panel
        yield self.completion_menu
        yield self.input_frame
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
