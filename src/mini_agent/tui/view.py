"""Full-screen Textual view with a scrollable transcript and fixed input row."""

from __future__ import annotations

import asyncio
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
from mini_agent.runtime.user_input import OTHER_OPTION_LABEL

from .completion import CommandSuggestion, SlashCommandCompleter

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
        self._transcript_limit = transcript_limit
        self._completer = completer or SlashCommandCompleter()
        self._suggestions: list[CommandSuggestion] = []
        self._pending_chunks: list[str] = []
        self._pending_lock = Lock()
        self._flush_scheduled = False
        self._writes_closed = False
        self._follow_tail = True
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
        self.transcript = TranscriptTextArea(
            read_only=True,
            soft_wrap=True,
            show_cursor=False,
            show_line_numbers=False,
            id="transcript",
        )
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
        if self._is_running_status(self._status):
            self._choose_running_status()
        self._render_status()
        self.input.focus()
        if self._is_running_status(self._status):
            self._schedule_running_status()
        if self._pending_chunks:
            self._schedule_flush()

    @property
    def follow_tail(self) -> bool:
        return self._follow_tail

    @property
    def transcript_text(self) -> str:
        return self.transcript.text

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
        self._refresh_status()

    def scroll_page_down(self) -> None:
        self.transcript.scroll_page_down(animate=False)
        self.call_after_refresh(self._resume_follow_if_at_end)

    def follow_latest(self) -> None:
        self._follow_tail = True
        self.transcript.scroll_end(animate=False)
        self.call_after_refresh(self.transcript.scroll_end, animate=False)
        self._refresh_status()

    def pause_following(self) -> None:
        self._follow_tail = False
        self._refresh_status()

    def copy_transcript_selection(self) -> bool:
        selected = self.transcript.selected_text
        if not selected:
            self.input.focus()
            return False
        selection_end = self.transcript.selection.end
        self.copy_to_clipboard(selected)
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
        old_scroll = self.transcript.scroll_y
        combined = f"{self.transcript.text}{value}"
        if len(combined) > self._transcript_limit:
            keep = max(0, self._transcript_limit - len(_OMITTED_MARKER))
            combined = f"{_OMITTED_MARKER}{combined[-keep:]}" if keep else ""
            self.transcript.load_text(combined)
        else:
            self.transcript.insert(value, self.transcript.document.end)
        if self._follow_tail:
            self.transcript.scroll_end(animate=False)
        else:
            self.transcript.scroll_to(y=old_scroll, animate=False)
        self.call_after_refresh(self._sync_transcript_scroll, old_scroll)

    def _sync_transcript_scroll(self, previous_scroll: float) -> None:
        if self._follow_tail:
            self.transcript.scroll_end(animate=False)
        else:
            self.transcript.scroll_to(y=previous_scroll, animate=False)

    def _clear_now(self) -> None:
        with self._pending_lock:
            self._pending_chunks = []
            self._flush_scheduled = False
        self.transcript.load_text("")
        self._follow_tail = True
        self._refresh_status()

    def _resume_follow_if_at_end(self) -> None:
        if self.transcript.scroll_y >= self.transcript.max_scroll_y:
            self._follow_tail = True
            self._refresh_status()

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
