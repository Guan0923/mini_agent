"""Full-screen Textual view with a scrollable transcript and fixed input row."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from threading import Lock

from textual import events, on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.message import Message
from textual.timer import Timer
from textual.widgets import OptionList, Static, TextArea
from textual.widgets.option_list import Option
from textual.widgets.text_area import Selection

from mini_agent.runtime.contracts import UserQuestion
from mini_agent.runtime.user_input import OTHER_OPTION_LABEL

from .completion import CommandSuggestion, SlashCommandCompleter

_FLUSH_INTERVAL_SECONDS = 1 / 30
_COPY_NOTICE_SECONDS = 1.5
_OMITTED_MARKER = "[Earlier terminal output omitted]\n"


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
    #status { height: 1; padding: 0 1; background: #263442; color: #9fc3e8; }
    #completion-menu { height: auto; max-height: 8; display: none; }
    #question-header {
        height: auto;
        max-height: 4;
        display: none;
        padding: 0 1;
        color: #d7dde5;
        background: #171c21;
    }
    #question-menu {
        height: auto;
        max-height: 8;
        display: none;
        background: #171c21;
    }
    #input {
        width: 100%;
        height: 1;
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
        self._questions: tuple[UserQuestion, ...] = ()
        self._question_index = 0
        self._question_answers: dict[str, list[str]] = {}
        self._questionnaire_custom_input = False
        self._questionnaire_callback: Callable[[dict[str, list[str]]], None] | None = None
        self._input_before_questionnaire = ""
        self._placeholder_before_questionnaire = ""
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
        self.question_header = Static(id="question-header")
        self.question_menu = OptionList(id="question-menu")
        self.completion_menu = OptionList(id="completion-menu")
        self.input = TerminalInput(
            soft_wrap=True,
            show_line_numbers=False,
            id="input",
        )

    def compose(self) -> ComposeResult:
        yield self.transcript
        yield self.question_header
        yield self.question_menu
        yield self.completion_menu
        yield self.input
        yield self.status_line

    def on_mount(self) -> None:
        self._owner_loop = asyncio.get_running_loop()
        self.status_line.update(f" {self._status}")
        self.input.focus()
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
            self._status = status
            self._interrupt_enabled = interrupt_enabled
            self._refresh_status()

        self._run_on_owner(update)

    @property
    def questionnaire_active(self) -> bool:
        return bool(self._questions)

    @property
    def questionnaire_custom_input(self) -> bool:
        return self._questionnaire_custom_input

    def begin_questionnaire(
        self,
        questions: tuple[UserQuestion, ...],
        on_complete: Callable[[dict[str, list[str]]], None],
    ) -> None:
        if not questions:
            raise ValueError("Questionnaire requires at least one question.")

        def begin() -> None:
            if self._questions:
                raise RuntimeError("Only one questionnaire can be active at a time.")
            self._questions = questions
            self._question_index = 0
            self._question_answers = {}
            self._questionnaire_custom_input = False
            self._questionnaire_callback = on_complete
            self._input_before_questionnaire = self.input.value
            self._placeholder_before_questionnaire = self.input.placeholder
            self.input.value = ""
            self.input.placeholder = "Up/Down select | Enter confirm | Tab custom answer"
            self._hide_completions()
            self._render_question()
            self.input.focus()

        self._run_on_owner(begin)

    def cancel_questionnaire(self) -> None:
        def cancel() -> None:
            if self._questions:
                self._clear_questionnaire()

        self._run_on_owner(cancel)

    def stop(self) -> None:
        def close() -> None:
            self.flush_now()
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
        if self.questionnaire_active:
            self._hide_completions()
            if not self._questionnaire_custom_input and self.input.value:
                self.input.value = ""
            return
        self._suggestions = self._completer.suggestions(self.input.value, self.input.cursor_position)
        self.completion_menu.clear_options()
        self.completion_menu.add_options(
            Option(f"{item.value} — {item.description}", id=str(index)) for index, item in enumerate(self._suggestions)
        )
        self.completion_menu.display = bool(self._suggestions)
        if self._suggestions:
            self.completion_menu.highlighted = 0

    @on(TerminalInput.Submitted)
    def on_input_submitted(self, event: TerminalInput.Submitted) -> None:
        if event.input is not self.input:
            return
        if self.questionnaire_active:
            if self._questionnaire_custom_input:
                answer = event.value.strip()
                if not answer:
                    self.input.placeholder = "Answer cannot be empty"
                    return
                self._accept_question_answer(answer)
            else:
                self._accept_highlighted_question_option()
            return
        if self.completion_menu.display and self._suggestions:
            self._accept_completion()
            return
        value = event.value
        self.input.value = ""
        self.submissions.put_nowait(value)

    def _resize_input(self) -> None:
        self.input.styles.height = max(1, min(self.input.wrapped_document.height, 4))

    def on_key(self, event: events.Key) -> None:
        if event.key == "escape" and self._interrupt_enabled:
            if self.interrupts.empty():
                self.interrupts.put_nowait(None)
            event.prevent_default()
            event.stop()
            return
        if self.questionnaire_active:
            if self._questionnaire_custom_input:
                if event.key != "escape":
                    return
                self._questionnaire_custom_input = False
                self.input.value = ""
                self.input.placeholder = "Up/Down select | Enter confirm | Tab custom answer"
                self.input.focus()
                event.prevent_default()
                event.stop()
                return
            if event.key in {"down", "up"}:
                option_count = len(self.question_menu._options)
                current = self.question_menu.highlighted or 0
                step = 1 if event.key == "down" else -1
                self.question_menu.highlighted = (current + step) % option_count
            elif event.key == "tab":
                if self.question_menu.highlighted == len(self.question_menu._options) - 1:
                    self._questionnaire_custom_input = True
                    self.input.value = ""
                    self.input.placeholder = "Enter your answer"
                self.input.focus()
            else:
                return
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

    def _accept_completion(self) -> None:
        index = self.completion_menu.highlighted or 0
        suggestion = self._suggestions[index]
        end = self.input.cursor_position
        self.input.value = f"{self.input.value[: suggestion.start_position]}{suggestion.value}{self.input.value[end:]}"
        self.input.cursor_position = suggestion.start_position + len(suggestion.value)
        self._hide_completions()
        self.input.focus()

    def _hide_completions(self) -> None:
        self.completion_menu.display = False
        self._suggestions = []

    def _render_question(self) -> None:
        question = self._questions[self._question_index]
        self.question_header.update(
            f"PLAN QUESTION {self._question_index + 1}/{len(self._questions)} | {question.header}\n{question.question}"
        )
        self.question_header.display = True
        self.question_menu.clear_options()
        self.question_menu.add_options(
            [
                *(
                    Option(f"{option.label} - {option.description}", id=str(index))
                    for index, option in enumerate(question.options)
                ),
                Option(OTHER_OPTION_LABEL, id="other"),
            ]
        )
        self.question_menu.highlighted = 0
        self.question_menu.display = True

    def _accept_highlighted_question_option(self) -> None:
        question = self._questions[self._question_index]
        selected = self.question_menu.highlighted or 0
        if selected >= len(question.options):
            self.input.placeholder = "Select Other and press Tab to enter an answer"
            return
        self._accept_question_answer(question.options[selected].label)

    def _accept_question_answer(self, answer: str) -> None:
        question = self._questions[self._question_index]
        self._question_answers[question.id] = [answer]
        self._questionnaire_custom_input = False
        self.input.value = ""
        self._question_index += 1
        if self._question_index < len(self._questions):
            self.input.placeholder = "Up/Down select | Enter confirm | Tab custom answer"
            self._render_question()
            self.input.focus()
            return
        callback = self._questionnaire_callback
        answers = dict(self._question_answers)
        self._clear_questionnaire()
        if callback is not None:
            callback(answers)

    def _clear_questionnaire(self) -> None:
        previous_input = self._input_before_questionnaire
        previous_placeholder = self._placeholder_before_questionnaire
        self._questions = ()
        self._question_index = 0
        self._question_answers = {}
        self._questionnaire_custom_input = False
        self._questionnaire_callback = None
        self.question_header.display = False
        self.question_menu.display = False
        self.question_menu.clear_options()
        self.input.value = previous_input
        self.input.placeholder = previous_placeholder
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

    def _render_status(self) -> None:
        suffix = " | PgUp/PgDn scroll" if not self._follow_tail else ""
        self.status_line.update(f" {self._status}{suffix}")

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
