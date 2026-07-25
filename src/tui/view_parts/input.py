"""Keyboard, completion, questionnaire, and interrupt routing."""

from __future__ import annotations

from textual import events
from textual.timer import Timer
from textual.widgets import Input, ListView, TextArea
from textual.widgets.option_list import Option

from ..widgets import InlineChoiceList, TerminalInput
from .status import COPY_NOTICE_SECONDS


class ViewInputMixin:
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
            Option(f"{item.value} — {item.description}", id=str(index)) for index, item in enumerate(self._suggestions)
        )
        self.completion_menu.display = bool(self._suggestions)
        if self._suggestions:
            self.completion_menu.highlighted = 0

    def on_terminal_input_submitted(self, event: TerminalInput.Submitted) -> None:
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

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        self._handle_choice_selected(event)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._handle_choice_input_submitted(event)

    def _request_interrupt(self) -> None:
        if self.interrupts.empty():
            self.interrupts.put_nowait(None)

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

        timer = self.set_timer(COPY_NOTICE_SECONDS, restore)
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
