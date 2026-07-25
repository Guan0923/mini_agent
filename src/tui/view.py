"""Full-screen Textual view with a scrollable transcript and fixed input row."""

from __future__ import annotations

import asyncio
import random
from collections.abc import Callable
from threading import Lock
from traceback import format_exception

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.timer import Timer
from textual.widgets import OptionList, Static

from .components.choice_prompt import ChoicePromptMixin
from .components.completion import CommandSuggestion, SlashCommandCompleter
from .latex import LatexMarkdown
from .rendering.transcript import CompactProgress, TranscriptNode
from .rendering.transcript_rendering import TranscriptRenderingMixin
from .screens.diagnostic_mixin import TuiDiagnosticMixin
from .screens.diagnostics import DiagnosticSink
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
    .processing-progress, .transcript-tool-summary {
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
    #choice-panel {
        width: 1fr;
        height: auto;
        max-height: 40%;
        display: none;
        margin: 0 1;
        padding: 1 2;
        background: #17233a;
        border-left: thick #4f8cff;
        border-right: solid #314a6e;
        overflow-y: auto;
    }
    #choice-header {
        width: 1fr;
        height: auto;
        max-height: 4;
        display: none;
        padding: 0 0 1 0;
        color: #f0f6ff;
        background: transparent;
        text-align: left;
    }
    #review-details {
        width: 1fr;
        height: auto;
        max-height: 8;
        display: none;
        padding: 0 0 1 0;
        background: transparent;
        text-align: left;
        overflow-y: auto;
    }
    #queued-messages {
        height: auto;
        max-height: 6;
        margin: 0 1;
        padding: 0 1;
        background: #1f2630;
        color: #c7d6e8;
        border-left: solid #d9a441;
        overflow-y: auto;
    }
    .choice-list {
        width: 1fr;
        height: auto;
        max-height: 6;
        display: none;
        background: transparent;
    }
    .choice-row {
        width: 1fr;
        height: auto;
        padding: 0 1;
        color: #d7dde5;
        background: #1b2a42;
        border-left: solid #314a6e;
    }
    .choice-row.-highlighted-choice {
        color: #ffffff;
        background: #2368a2;
        border-left: thick #f0c36a;
    }
    .choice-row.-selected-answer {
        color: #102033;
        background: #84d2bd;
        border-left: thick #e8f6ed;
    }
    .choice-label {
        width: 1fr;
        height: auto;
        text-align: left;
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
    #input-frame {
        width: 100%;
        height: auto;
        margin-bottom: 0;
        background: #171c21;
    }
    #input-frame.-single-line {
        outline: solid #405675;
        background: #1b2736;
    }
    #input-frame.-multiline {
        outline: none;
        background: #171c21;
    }
    #input {
        width: 100%;
        height: 1;
        min-height: 1;
        margin-bottom: 0;
        border: none;
        padding: 0;
        background: transparent;
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
        self.choice_panel = VerticalScroll(self.question_header, self.review_details, id="choice-panel")
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
