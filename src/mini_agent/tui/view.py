"""Full-screen Textual view with a scrollable transcript and fixed input row."""

from __future__ import annotations

import asyncio
import json
import random
from collections.abc import Callable
from dataclasses import dataclass
from threading import Lock
from typing import Literal

from rich.text import Text
from textual import events, on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.message import Message
from textual.timer import Timer
from textual.widgets import Input, ListItem, ListView, OptionList, Static, TextArea
from textual.widgets.option_list import Option
from textual.widgets.text_area import Selection

from mini_agent.runtime.contracts import UserQuestion
from mini_agent.runtime.events import RuntimeEvent
from mini_agent.runtime.user_input import OTHER_OPTION_LABEL

from .completion import CommandSuggestion, SlashCommandCompleter
from .transcript import MarkdownBody, StatusLeaf, TranscriptNode, TranscriptScroll

_FLUSH_INTERVAL_SECONDS = 1 / 30
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
_OMITTED_MARKER = "[Earlier terminal output omitted]\n"


@dataclass(frozen=True)
class ChoiceItem:
    id: str
    label: str
    description: str = ""
    custom: bool = False


@dataclass
class _ToolTranscript:
    node: TranscriptNode
    arguments: MarkdownBody
    result: MarkdownBody
    status: StatusLeaf


class ChoiceRow(ListItem):
    """One selectable choice that can replace its label with an inline editor."""

    def __init__(self, choice: ChoiceItem) -> None:
        self.choice = choice
        text = f"{choice.label} - {choice.description}" if choice.description else choice.label
        self.label = Static(text, classes="choice-label")
        self.editor = Input(placeholder="Enter your answer", classes="choice-editor")
        self.editor.display = False
        super().__init__(self.label, self.editor, classes="choice-row")

    def begin_edit(self, value: str = "") -> None:
        self.label.display = False
        self.editor.value = value
        self.editor.placeholder = "Enter your answer"
        self.editor.display = True
        self.editor.focus()

    def end_edit(self) -> None:
        self.editor.value = ""
        self.editor.display = False
        self.label.display = True


class InlineChoiceList(ListView, can_focus_children=True):
    """ListView variant whose custom rows may focus an embedded Input."""

    def __init__(self, items: tuple[ChoiceItem, ...], *, question_index: int | None = None) -> None:
        self.question_index = question_index
        self.rows = tuple(ChoiceRow(item) for item in items)
        super().__init__(*self.rows, initial_index=0, classes="choice-list")

    @property
    def highlighted_row(self) -> ChoiceRow | None:
        child = self.highlighted_child
        return child if isinstance(child, ChoiceRow) else None

    def action_cursor_down(self) -> None:
        if self.index is not None and self.index == len(self.rows) - 1:
            self.index = 0
            return
        super().action_cursor_down()

    def action_cursor_up(self) -> None:
        if self.index == 0:
            self.index = len(self.rows) - 1
            return
        super().action_cursor_up()


class TranscriptTextArea(TextArea):
    """Read-only text area that keeps right-click copy local to the transcript."""

    def on_mouse_down(self, event: events.MouseDown) -> None:
        if event.button == 3:
            view = self.app
            if isinstance(view, TerminalView):
                view.copy_transcript_selection()
            event.prevent_default()
            event.stop()

    def _on_mouse_scroll_up(self, event: events.MouseScrollUp) -> None:
        view = self.app
        if isinstance(view, TerminalView):
            view.pause_following()
        super()._on_mouse_scroll_up(event)

    def _on_mouse_scroll_down(self, event: events.MouseScrollDown) -> None:
        super()._on_mouse_scroll_down(event)
        view = self.app
        if isinstance(view, TerminalView):
            view.call_after_refresh(view._resume_follow_if_at_end)


class TerminalInput(TextArea):
    """Multiline editor that keeps the value/cursor surface used by the TUI."""

    class Submitted(Message):
        def __init__(self, input: TerminalInput, value: str) -> None:
            super().__init__()
            self.input = input
            self.value = value

    @property
    def value(self) -> str:
        return self.text

    @value.setter
    def value(self, value: str) -> None:
        self.load_text(value)
        self.cursor_location = self.document.get_location_from_index(len(value))

    @property
    def cursor_position(self) -> int:
        return self.document.get_index_from_location(self.cursor_location)

    @cursor_position.setter
    def cursor_position(self, value: int) -> None:
        index = max(0, min(value, len(self.text)))
        self.cursor_location = self.document.get_location_from_index(index)

    def on_key(self, event: events.Key) -> None:
        if event.key == "ctrl+j":
            result = self.replace("\n", self.selection.start, self.selection.end)
            self.selection = Selection.cursor(result.end_location)
        elif event.key == "enter":
            self.post_message(self.Submitted(self, self.value))
        else:
            return
        event.prevent_default()
        event.stop()

    def _on_paste(self, event: events.Paste) -> None:
        event.stop()


class TerminalView(App[None]):
    """Own the Textual widgets and expose the small interface used by TerminalApp."""

    CSS = """
    Screen { background: #101418; color: #d7dde5; }
    #transcript {
        height: 1fr;
        border: none;
        padding: 0 1;
        background: #101418;
        color: #d7dde5;
        overflow-y: scroll;
    }
    .transcript-node {
        width: 1fr;
        height: auto;
        background: #101418;
        padding: 0;
    }
    .transcript-node > Contents {
        padding: 0 0 0 2;
    }
    .transcript-status {
        padding-left: 2;
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
        status_random: random.Random | None = None,
    ) -> None:
        super().__init__()
        self._owner_loop = loop
        self._owner_ready = False
        self._pending_owner_callbacks: list[Callable[[], None]] = []
        self._transcript_limit = transcript_limit
        self._completer = completer or SlashCommandCompleter()
        self._suggestions: list[CommandSuggestion] = []
        self._pending_chunks: list[str] = []
        self._pending_lock = Lock()
        self._flush_scheduled = False
        self._writes_closed = False
        self._follow_tail = True
        self._paused_scroll_y = 0.0
        self._status = "AGENT | IDLE"
        self._interrupt_enabled = False
        self._copy_notice_timer: Timer | None = None
        self._choice_kind: Literal["question", "review"] | None = None
        self._questions: tuple[UserQuestion, ...] = ()
        self._status_random = status_random or random.Random()
        self._status_timer: Timer | None = None
        self._running_status: str | None = None
        self._choice_lists: list[InlineChoiceList] = []
        self._question_index = 0
        self._question_answers: dict[str, list[str]] = {}
        self._question_selections: dict[str, str] = {}
        self._questionnaire_callback: Callable[[dict[str, list[str]]], None] | None = None
        self._review_callback: Callable[[str, str | None], None] | None = None
        self.submissions: asyncio.Queue[str | None] = asyncio.Queue()
        self.interrupts: asyncio.Queue[None] = asyncio.Queue()
        self.transcript = TranscriptScroll()
        self.transcript_nodes: list[TranscriptNode] = []
        self.markdown_bodies: list[MarkdownBody] = []
        self._top_level_nodes: list[TranscriptNode] = []
        self._top_level_bodies: dict[TranscriptNode, list[MarkdownBody]] = {}
        self._node_top_level: dict[TranscriptNode, TranscriptNode] = {}
        self._completed_top_levels: set[TranscriptNode] = set()
        self._pending_assistants: list[TranscriptNode] = []
        self._assistant_by_run: dict[str, TranscriptNode] = {}
        self._thinking_by_run: dict[str, tuple[TranscriptNode, MarkdownBody]] = {}
        self._response_by_run: dict[str, tuple[TranscriptNode, MarkdownBody]] = {}
        self._tools_by_call: dict[tuple[str, str], _ToolTranscript] = {}
        self._seen_exchanges: set[tuple[str, str]] = set()
        self._last_response_by_run: dict[str, str] = {}
        self._streaming_system: tuple[TranscriptNode, MarkdownBody] | None = None
        self.status_line = Static(id="status")
        self.context_progress = ContextProgress()
        self.question_header = Static(id="choice-header")
        self.completion_menu = OptionList(id="completion-menu")
        self.input = TerminalInput(
            soft_wrap=True,
            show_line_numbers=False,
            id="input",
        )

    def compose(self) -> ComposeResult:
        yield self.transcript
        yield self.question_header
        yield self.completion_menu
        yield self.input
        yield Horizontal(self.status_line, self.context_progress, id="status-bar")

    def on_mount(self) -> None:
        self._owner_loop = asyncio.get_running_loop()
        self._owner_ready = True
        if self._pending_owner_callbacks:
            self.call_after_refresh(self._flush_pending_owner_callbacks)
        if self._is_running_status(self._status):
            self._choose_running_status()
        self._render_status()
        self.input.focus()
        if self._is_running_status(self._status):
            self._schedule_running_status()
        if self._pending_chunks:
            self._schedule_flush()

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
            self._pending_assistants.append(assistant)
            self._streaming_system = None
            self._scroll_after_transcript_change()

        self._run_on_owner(begin)

    def write_system(self, text: str, end: str = "\n") -> None:
        """Render non-conversation output as a top-level SYSTEM branch."""
        value = f"{text}{end}"
        if not value or self._writes_closed:
            return

        def write_system() -> None:
            self.transcript.append_text(value)
            if self._streaming_system is None:
                system = self._new_top_level("SYSTEM")
                body = MarkdownBody("")
                self._register_body(body, system)
                system.add_node(body)
                self._streaming_system = (system, body)
            self._streaming_system[1].append_markdown(value)
            if end:
                self._completed_top_levels.add(self._streaming_system[0])
                self._streaming_system = None
            self._scroll_after_transcript_change()

        self._run_on_owner(write_system)

    def load_history(self, messages: list[dict[str, str]]) -> None:
        """Replace the rendered transcript with persisted user and assistant messages."""
        def load() -> None:
            self._reset_transcript_state()
            for message in messages:
                role = message.get("role", "system").lower()
                title = "USER" if role == "user" else "ASSISTANT" if role == "assistant" else "SYSTEM"
                node = self._new_top_level(title, completed=True)
                body = MarkdownBody(message.get("content", ""))
                self._register_body(body, node)
                node.add_node(body)
            self._scroll_after_transcript_change()

        self._run_on_owner(load)

    def handle_runtime_event(self, event: RuntimeEvent) -> None:
        """Route runtime events into their run's ordered ASSISTANT branch."""
        self._run_on_owner(lambda: self._handle_runtime_event_now(event))

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

    @property
    def questionnaire_active(self) -> bool:
        return self._choice_kind == "question"

    @property
    def questionnaire_custom_input(self) -> bool:
        return self._editing_row() is not None

    @property
    def choice_menu(self) -> InlineChoiceList:
        if self._choice_kind is None:
            raise RuntimeError("No choice prompt is active.")
        return self._active_choice_list()

    @property
    def question_menu(self) -> InlineChoiceList:
        if not self.questionnaire_active:
            raise RuntimeError("No questionnaire is active.")
        return self.choice_menu

    @property
    def question_lists(self) -> tuple[InlineChoiceList, ...]:
        return tuple(self._choice_lists) if self.questionnaire_active else ()

    @property
    def question_index(self) -> int:
        return self._question_index

    def begin_questionnaire(
        self,
        questions: tuple[UserQuestion, ...],
        on_complete: Callable[[dict[str, list[str]]], None],
    ) -> None:
        if not questions:
            raise ValueError("Questionnaire requires at least one question.")

        def begin() -> None:
            self._ensure_no_choice_prompt()
            self._choice_kind = "question"
            self._questions = questions
            self._question_index = 0
            self._question_answers = {}
            self._question_selections = {}
            self._questionnaire_callback = on_complete
            lists = [
                InlineChoiceList(
                    (
                        *(
                            ChoiceItem(str(option_index), option.label, option.description)
                            for option_index, option in enumerate(question.options)
                        ),
                        ChoiceItem("other", OTHER_OPTION_LABEL, custom=True),
                    ),
                    question_index=question_index,
                )
                for question_index, question in enumerate(questions)
            ]
            self._mount_choice_lists(lists)
            self._show_question(0)

        self._run_on_owner(begin)

    def begin_review(
        self,
        title: str,
        prompt: str,
        choices: tuple[ChoiceItem, ...],
        on_complete: Callable[[str, str | None], None],
    ) -> None:
        if not choices:
            raise ValueError("Review requires at least one choice.")

        def begin() -> None:
            self._ensure_no_choice_prompt()
            self._choice_kind = "review"
            self._review_callback = on_complete
            self.question_header.update(f"{title}\n{prompt}")
            self.question_header.display = True
            lists = [InlineChoiceList(choices)]
            self._mount_choice_lists(lists)
            self._show_choice_list(0)

        self._run_on_owner(begin)

    def cancel_questionnaire(self) -> None:
        self.cancel_choice_prompt()

    def cancel_choice_prompt(self) -> None:
        def cancel() -> None:
            if self._choice_kind is not None:
                self._clear_choice_prompt()

        self._run_on_owner(cancel)

    def _ensure_no_choice_prompt(self) -> None:
        if self._choice_kind is not None:
            raise RuntimeError("Only one terminal choice prompt can be active at a time.")

    def _mount_choice_lists(self, lists: list[InlineChoiceList]) -> None:
        self._choice_lists = lists
        for choice_list in lists:
            choice_list.display = False
        self._hide_completions()
        self.screen.mount(*lists, before=self.completion_menu)

    def stop(self) -> None:
        def close() -> None:
            self.flush_now()
            self._stop_running_status()
            self._writes_closed = True
            if self.is_running:
                self.exit()

        self._run_on_owner(close)

    def flush_now(self) -> None:
        chunks = self._take_pending_chunks()
        if chunks:
            self._append_transcript("".join(chunks))

    def scroll_page_up(self) -> None:
        self._follow_tail = False
        self.transcript.scroll_page_up(animate=False)
        self._remember_paused_scroll()
        self._refresh_status()

    def scroll_page_down(self) -> None:
        self.transcript.scroll_page_down(animate=False)
        self.call_after_refresh(self._resume_follow_if_at_end)

    def follow_latest(self) -> None:
        self._follow_tail = True
        self._paused_scroll_y = 0.0
        self.transcript.scroll_end(animate=False)
        self.call_after_refresh(self._settle_follow_latest)
        self._refresh_status()

    def _settle_follow_latest(self) -> None:
        if not self._follow_tail:
            return
        self.transcript.scroll_end(animate=False)
        self.call_after_refresh(self.transcript.scroll_end, animate=False)

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

    def action_control_c(self) -> None:
        self.submissions.put_nowait("/quit")

    def action_control_d(self) -> None:
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
        choice_list = event.list_view
        if not isinstance(choice_list, InlineChoiceList) or choice_list not in self._choice_lists:
            return
        row = event.item
        if not isinstance(row, ChoiceRow) or row.choice.custom:
            return
        self._accept_choice(choice_list, row, None)

    @on(Input.Submitted)
    def on_choice_input_submitted(self, event: Input.Submitted) -> None:
        editing = self._editing_row()
        if editing is None or event.input is not editing.editor:
            return
        value = event.value.strip()
        if not value:
            editing.editor.placeholder = "Answer cannot be empty"
            return
        choice_list = self._choice_list_for_row(editing)
        editing.end_edit()
        self._accept_choice(choice_list, editing, value)

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

    def _show_question(self, index: int) -> None:
        self._question_index = index
        question = self._questions[index]
        self.question_header.update(
            f"PLAN QUESTION {index + 1}/{len(self._questions)} | {question.header}\n{question.question}"
        )
        self.question_header.display = True
        self._show_choice_list(index)

    def _show_choice_list(self, index: int) -> None:
        for list_index, choice_list in enumerate(self._choice_lists):
            choice_list.display = list_index == index
        self.call_after_refresh(self._choice_lists[index].focus)

    def _move_question(self, step: int) -> None:
        target = self._question_index + step
        if 0 <= target < len(self._questions):
            self._show_question(target)

    def _active_choice_list(self) -> InlineChoiceList:
        index = self._question_index if self.questionnaire_active else 0
        return self._choice_lists[index]

    def _editing_row(self) -> ChoiceRow | None:
        for choice_list in self._choice_lists:
            for row in choice_list.rows:
                if row.editor.display:
                    return row
        return None

    def _choice_list_for_row(self, target: ChoiceRow) -> InlineChoiceList:
        for choice_list in self._choice_lists:
            if target in choice_list.rows:
                return choice_list
        raise RuntimeError("Inline choice row is not attached to the active prompt.")

    def _existing_custom_answer(self, choice_list: InlineChoiceList) -> str:
        if not self.questionnaire_active or choice_list.question_index is None:
            return ""
        question = self._questions[choice_list.question_index]
        if self._question_selections.get(question.id) != "other":
            return ""
        return self._question_answers.get(question.id, [""])[0]

    def _accept_choice(
        self,
        choice_list: InlineChoiceList,
        row: ChoiceRow,
        custom_value: str | None,
    ) -> None:
        if self._choice_kind == "review":
            callback = self._review_callback
            choice_id = row.choice.id
            self._clear_choice_prompt()
            if callback is not None:
                callback(choice_id, custom_value)
            return

        if self._choice_kind != "question" or choice_list.question_index is None:
            return
        question_index = choice_list.question_index
        question = self._questions[question_index]
        answer = custom_value if custom_value is not None else row.choice.label
        self._question_answers[question.id] = [answer]
        self._question_selections[question.id] = row.choice.id
        for candidate in choice_list.rows:
            candidate.set_class(candidate is row, "-selected-answer")

        unanswered = [
            index for index, item in enumerate(self._questions) if item.id not in self._question_answers
        ]
        if not unanswered:
            callback = self._questionnaire_callback
            answers = {item.id: self._question_answers[item.id] for item in self._questions}
            self._clear_choice_prompt()
            if callback is not None:
                callback(answers)
            return

        right = [index for index in unanswered if index > question_index]
        target = right[0] if right else max(index for index in unanswered if index < question_index)
        self._show_question(target)

    def _clear_choice_prompt(self) -> None:
        for choice_list in self._choice_lists:
            choice_list.display = False
            choice_list.remove()
        self._choice_kind = None
        self._questions = ()
        self._choice_lists = []
        self._question_index = 0
        self._question_answers = {}
        self._question_selections = {}
        self._questionnaire_callback = None
        self._review_callback = None
        self.question_header.display = False
        self.input.focus()

    def _schedule_flush(self) -> None:
        if self._writes_closed:
            return
        loop = self._owner_loop
        if loop is not None:
            loop.call_later(_FLUSH_INTERVAL_SECONDS, self._flush_pending)

    def _flush_pending(self) -> None:
        chunks = self._take_pending_chunks()
        if chunks:
            self._append_transcript("".join(chunks))

    def _take_pending_chunks(self) -> list[str]:
        with self._pending_lock:
            chunks = self._pending_chunks
            self._pending_chunks = []
            self._flush_scheduled = False
        return chunks

    def _append_transcript(self, value: str) -> None:
        old_scroll = self.transcript.scroll_y if self._follow_tail else self._paused_scroll_y
        combined = f"{self.transcript.text}{value}"
        if len(combined) > self._transcript_limit:
            keep = max(0, self._transcript_limit - len(_OMITTED_MARKER))
            combined = f"{_OMITTED_MARKER}{combined[-keep:]}" if keep else ""
            self.transcript.load_text(combined)
        else:
            self.transcript.append_text(value)
        if self._follow_tail:
            self.transcript.scroll_end(animate=False)
        else:
            self.transcript.scroll_to(y=old_scroll, animate=False)
        self.call_after_refresh(self._sync_transcript_scroll, old_scroll)

    def _new_top_level(self, title: str, *, completed: bool = False) -> TranscriptNode:
        node = TranscriptNode(title, collapsed=title == "SYSTEM")
        self.transcript_nodes.append(node)
        self._top_level_nodes.append(node)
        self._top_level_bodies[node] = []
        self._node_top_level[node] = node
        if completed:
            self._completed_top_levels.add(node)
        self.transcript.add_top_level(node)
        return node

    def _add_assistant_node(
        self,
        assistant: TranscriptNode,
        title: str,
        *,
        collapsed: bool,
        markdown: str = "",
    ) -> tuple[TranscriptNode, MarkdownBody]:
        body = MarkdownBody(markdown)
        node = TranscriptNode(title, body, collapsed=collapsed)
        self._register_body(body, assistant)
        self.transcript_nodes.append(node)
        self._node_top_level[node] = assistant
        assistant.add_node(node)
        return node, body

    def _register_body(self, body: MarkdownBody, top_level: TranscriptNode) -> None:
        self.markdown_bodies.append(body)
        self._top_level_bodies[top_level].append(body)

    def _assistant_for_run(self, run_id: str) -> TranscriptNode | None:
        assistant = self._assistant_by_run.get(run_id)
        if assistant is None and self._pending_assistants:
            assistant = self._pending_assistants.pop(0)
            self._assistant_by_run[run_id] = assistant
        return assistant

    def _append_system_output(self, value: str) -> None:
        if self._streaming_system is None:
            system = self._new_top_level("SYSTEM")
            body = MarkdownBody("")
            self._register_body(body, system)
            system.add_node(body)
            self._streaming_system = (system, body)
        self._streaming_system[1].append_markdown(value)
        if value.endswith("\n"):
            self._completed_top_levels.add(self._streaming_system[0])
            self._streaming_system = None

    def _handle_runtime_event_now(self, event: RuntimeEvent) -> None:
        data = event.data
        run_id = str(data.get("run_id", ""))
        if event.kind == "run_started":
            if run_id:
                self._assistant_for_run(run_id)
            return
        if not run_id:
            return
        assistant = self._assistant_for_run(run_id)
        if assistant is None:
            return
        self._streaming_system = None
        if event.kind == "thinking_start":
            node, body = self._add_assistant_node(assistant, "think_content", collapsed=True)
            node.set_activity(True)
            self._thinking_by_run[run_id] = (node, body)
        elif event.kind == "thinking_delta":
            current = self._thinking_by_run.get(run_id)
            if current is not None:
                current[1].append_markdown(event.message)
        elif event.kind == "thinking_end":
            current = self._thinking_by_run.pop(run_id, None)
            if current is not None:
                current[0].set_activity(False)
        elif event.kind == "response_start":
            node, body = self._add_assistant_node(assistant, "response_content", collapsed=False)
            node.set_activity(True)
            self._response_by_run[run_id] = (node, body)
        elif event.kind == "response_delta":
            current = self._response_by_run.get(run_id)
            if current is not None:
                current[1].append_markdown(event.message)
                self._last_response_by_run[run_id] = current[1].markdown_text
        elif event.kind == "response_end":
            current = self._response_by_run.pop(run_id, None)
            if current is not None:
                current[0].set_activity(False)
                self._last_response_by_run[run_id] = current[1].markdown_text
        elif event.kind == "assistant_message":
            self._handle_assistant_message(run_id, assistant, data)
        elif event.kind == "tool_call":
            self._handle_tool_call(run_id, assistant, event.message, data)
        elif event.kind in {"tool_result", "tool_failed"}:
            self._handle_tool_completion(run_id, assistant, event.kind, event.message, data)
        elif event.kind in {"retry", "tool_recovery"}:
            tool = self._tool_for_event(run_id, assistant, event.message, data)
            if tool is not None:
                tool.status.set_status("running")
                tool.node.set_activity(True)
        elif event.kind in {"response", "plan"}:
            if event.message != self._last_response_by_run.get(run_id, ""):
                _, body = self._add_assistant_node(
                    assistant, "response_content", collapsed=False, markdown=event.message
                )
                self._last_response_by_run[run_id] = body.markdown_text
        elif event.kind in {"run_finished", "cancelled", "error", "model_error"}:
            self._stop_run_activity(run_id)
            if event.message:
                self._add_assistant_node(assistant, event.kind, collapsed=False, markdown=event.message)
            self._completed_top_levels.add(assistant)
        elif event.kind not in {"model_request", "model_response", "context_usage"} and event.message:
            self._add_assistant_node(assistant, event.kind, collapsed=False, markdown=event.message)
        self._scroll_after_transcript_change()

    def _handle_assistant_message(
        self, run_id: str, assistant: TranscriptNode, data: dict[str, object]
    ) -> None:
        exchange_id = data.get("exchange_id")
        if isinstance(exchange_id, str) and (run_id, exchange_id) in self._seen_exchanges:
            return
        if isinstance(exchange_id, str):
            self._seen_exchanges.add((run_id, exchange_id))
        message = data.get("message")
        if not isinstance(message, dict):
            return
        reasoning = message.get("reasoning")
        if isinstance(reasoning, str) and reasoning and not data.get("reasoning_streamed"):
            self._add_assistant_node(assistant, "think_content", collapsed=True, markdown=reasoning)
        content = message.get("content")
        if isinstance(content, str) and content and not data.get("content_streamed"):
            if content != self._last_response_by_run.get(run_id, ""):
                _, body = self._add_assistant_node(
                    assistant, "response_content", collapsed=False, markdown=content
                )
                self._last_response_by_run[run_id] = body.markdown_text
        tools = message.get("tool_messages", ())
        if isinstance(tools, list):
            for tool_data in tools:
                if isinstance(tool_data, dict):
                    self._ensure_tool(run_id, assistant, tool_data)

    def _ensure_tool(
        self, run_id: str, assistant: TranscriptNode, data: dict[str, object]
    ) -> _ToolTranscript | None:
        call_id = data.get("call_id")
        if not isinstance(call_id, str) or not call_id:
            return None
        key = (run_id, call_id)
        existing = self._tools_by_call.get(key)
        if existing is not None:
            return existing
        name = str(data.get("name") or data.get("tool") or "tool")
        arguments = data.get("arguments", {})
        formatted = json.dumps(arguments, ensure_ascii=False, indent=2, default=str)
        argument_body = MarkdownBody(f"```json\n{formatted}\n```")
        result_body = MarkdownBody("")
        arguments_node = TranscriptNode("arguments", argument_body, collapsed=False)
        result_node = TranscriptNode("result", result_body, collapsed=False)
        status = StatusLeaf(str(data.get("status", "pending")))
        node = TranscriptNode(
            f"tool_call: {name}", arguments_node, result_node, status, collapsed=True
        )
        self._register_body(argument_body, assistant)
        self._register_body(result_body, assistant)
        self.transcript_nodes.append(node)
        self._node_top_level[node] = assistant
        assistant.add_node(node)
        tool = _ToolTranscript(node, argument_body, result_body, status)
        self._tools_by_call[key] = tool
        node.set_activity(status.status in {"pending", "running"})
        return tool

    def _tool_for_event(
        self, run_id: str, assistant: TranscriptNode, message: str, data: dict[str, object]
    ) -> _ToolTranscript | None:
        call_id = data.get("call_id")
        if isinstance(call_id, str) and call_id:
            tool = self._tools_by_call.get((run_id, call_id))
            if tool is not None:
                return tool
            details = dict(data)
            details.setdefault("name", message or data.get("tool", "tool"))
            return self._ensure_tool(run_id, assistant, details)
        return None

    def _handle_tool_call(
        self, run_id: str, assistant: TranscriptNode, message: str, data: dict[str, object]
    ) -> None:
        tool = self._tool_for_event(run_id, assistant, message, data)
        if tool is None:
            return
        if "arguments" in data:
            formatted = json.dumps(data["arguments"], ensure_ascii=False, indent=2, default=str)
            tool.arguments.set_markdown(f"```json\n{formatted}\n```")
        tool.status.set_status("running")
        tool.node.set_activity(True)

    def _handle_tool_completion(
        self,
        run_id: str,
        assistant: TranscriptNode,
        kind: str,
        message: str,
        data: dict[str, object],
    ) -> None:
        tool = self._tool_for_event(run_id, assistant, str(data.get("tool", "tool")), data)
        if tool is None:
            return
        tool.result.set_markdown(message)
        tool.status.set_status("succeeded" if kind == "tool_result" else "failed")
        tool.node.set_activity(False)

    def _stop_run_activity(self, run_id: str) -> None:
        current = self._thinking_by_run.pop(run_id, None)
        if current is not None:
            current[0].set_activity(False)
        current = self._response_by_run.pop(run_id, None)
        if current is not None:
            current[0].set_activity(False)
        for (tool_run_id, _), tool in self._tools_by_call.items():
            if tool_run_id == run_id:
                tool.node.set_activity(False)

    def _reset_transcript_state(self) -> None:
        self.transcript.clear_nodes(self._top_level_nodes)
        self.transcript_nodes = []
        self.markdown_bodies = []
        self._top_level_nodes = []
        self._top_level_bodies = {}
        self._node_top_level = {}
        self._completed_top_levels = set()
        self._pending_assistants = []
        self._assistant_by_run = {}
        self._thinking_by_run = {}
        self._response_by_run = {}
        self._tools_by_call = {}
        self._seen_exchanges = set()
        self._last_response_by_run = {}
        self._streaming_system = None

    def _scroll_after_transcript_change(self) -> None:
        self._trim_completed_top_levels()
        self.transcript.sync_text(self._structured_transcript_text())
        if self._follow_tail:
            self.call_after_refresh(self.transcript.scroll_end, animate=False)

    def _structured_transcript_text(self) -> str:
        sections: list[str] = []
        for node in self._top_level_nodes:
            sections.append(node.title_text)
            sections.extend(
                body.markdown_text
                for body in self._top_level_bodies.get(node, ())
                if body.markdown_text
            )
        return "\n".join(sections)

    def _trim_completed_top_levels(self) -> None:
        while len(self._structured_transcript_text()) > self._transcript_limit:
            candidate = next(
                (node for node in self._top_level_nodes if node in self._completed_top_levels),
                None,
            )
            if candidate is None:
                return
            self._remove_top_level(candidate)

    def _remove_top_level(self, node: TranscriptNode) -> None:
        if node.is_mounted:
            node.remove()
        self._top_level_nodes.remove(node)
        self._completed_top_levels.discard(node)
        removed_bodies = set(self._top_level_bodies.pop(node, ()))
        self.markdown_bodies = [body for body in self.markdown_bodies if body not in removed_bodies]
        removed_nodes = {
            child for child, top_level in self._node_top_level.items() if top_level is node
        }
        self.transcript_nodes = [child for child in self.transcript_nodes if child not in removed_nodes]
        for child in removed_nodes:
            self._node_top_level.pop(child, None)
        self._pending_assistants = [assistant for assistant in self._pending_assistants if assistant is not node]
        removed_runs = [run_id for run_id, assistant in self._assistant_by_run.items() if assistant is node]
        for run_id in removed_runs:
            self._assistant_by_run.pop(run_id, None)
            self._thinking_by_run.pop(run_id, None)
            self._response_by_run.pop(run_id, None)
            self._last_response_by_run.pop(run_id, None)
            self._seen_exchanges = {
                item for item in self._seen_exchanges if item[0] != run_id
            }
            self._tools_by_call = {
                key: tool for key, tool in self._tools_by_call.items() if key[0] != run_id
            }

    def _sync_transcript_scroll(self, previous_scroll: float) -> None:
        if self._follow_tail:
            self.transcript.scroll_end(animate=False)
        else:
            self.transcript.scroll_to(y=self._paused_scroll_y, animate=False)

    def _clear_now(self) -> None:
        with self._pending_lock:
            self._pending_chunks = []
            self._flush_scheduled = False
        self._reset_transcript_state()
        self.transcript.load_text("")
        self._follow_tail = True
        self._paused_scroll_y = 0.0
        self._refresh_status()

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

    def _run_on_owner(self, callback) -> None:
        if not self._owner_ready:
            self._pending_owner_callbacks.append(callback)
            return
        loop = self._owner_loop
        if loop is None or not loop.is_running():
            callback()
            return
        try:
            if asyncio.get_running_loop() is loop:
                callback()
                return
        except RuntimeError:
            pass
        loop.call_soon_threadsafe(callback)


class ContextProgress(Static):
    """One-line context usage meter with the compression threshold marked."""

    DEFAULT_CSS = "ContextProgress { width: 1fr; min-width: 1; height: 1; padding: 0 1; background: #263442; }"

    def __init__(self) -> None:
        super().__init__(id="context-progress")
        self.estimated_tokens: int | None = None
        self.context_size: int | None = None
        self.threshold = 0.8

    @property
    def ratio(self) -> float | None:
        if self.estimated_tokens is None or not self.context_size:
            return None
        return self.estimated_tokens / self.context_size

    def set_usage(
        self,
        estimated_tokens: int | None,
        context_size: int | None,
        threshold: float = 0.8,
    ) -> None:
        self.estimated_tokens = estimated_tokens
        self.context_size = context_size
        self.threshold = max(0.0, min(threshold, 1.0))
        self.refresh()

    def render(self) -> Text:
        width = max(1, self.size.width - 2)
        ratio = self.ratio
        if ratio is None:
            detailed = "CONTEXT N/A "
            compact = "CTX N/A "
        else:
            percent = ratio * 100
            detailed = f"CONTEXT {self.estimated_tokens:,} / {self.context_size:,} {percent:.0f}% "
            compact = f"CTX {percent:.0f}% "
        if width - len(detailed) >= 8:
            label = detailed
        elif width - len(compact) >= 4:
            label = compact
        elif width >= 8:
            label = "CTX "
        else:
            label = ""
        bar_width = max(1, width - len(label))
        marker = min(bar_width - 1, max(0, round(self.threshold * (bar_width - 1))))
        filled = 0 if ratio is None else min(bar_width, max(0, round(ratio * bar_width)))
        bar = ["━" if index < filled else "─" for index in range(bar_width)]
        bar[marker] = "┊"
        text = Text(label + "".join(bar), no_wrap=True, overflow="crop")
        text.stylize("#8a96a3", 0, len(label))
        fill_color = "#65b8a6"
        if ratio is not None and ratio >= 1:
            fill_color = "#e26464"
        elif ratio is not None and ratio >= self.threshold:
            fill_color = "#e3b65f"
        text.stylize("#47515b", len(label), len(text))
        for index in range(filled):
            if index != marker:
                text.stylize(fill_color, len(label) + index, len(label) + index + 1)
        text.stylize("#d7dde5 bold", len(label) + marker, len(label) + marker + 1)
        return text
