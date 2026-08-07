"""Questionnaire and approval-choice behavior for the terminal view."""

from __future__ import annotations

from collections.abc import Callable
from typing import Literal

from backend.runtime.conversation.user_input import OTHER_OPTION_LABEL
from backend.runtime.core.contracts import UserQuestion
from textual.widgets import Input, ListView

from ..widgets import ChoiceItem, ChoiceRow, InlineChoiceList


class ChoicePromptMixin:
    """Own inline questionnaire and approval prompt state and transitions."""

    def _init_choice_prompt_state(self) -> None:
        self._choice_kind: Literal["question", "review"] | None = None
        self._questions: tuple[UserQuestion, ...] = ()
        self._choice_lists: list[InlineChoiceList] = []
        self._question_index = 0
        self._question_answers: dict[str, list[str]] = {}
        self._question_selections: dict[str, str] = {}
        self._questionnaire_callback: Callable[[dict[str, list[str]]], None] | None = None
        self._review_callback: Callable[[str, str | None], None] | None = None

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
        summary: str,
        details: str,
        choices: tuple[ChoiceItem, ...],
        on_complete: Callable[[str, str | None], None],
        *,
        initial_choice_id: str | None = None,
    ) -> None:
        if not choices:
            raise ValueError("Review requires at least one choice.")

        def begin() -> None:
            self._ensure_no_choice_prompt()
            self._choice_kind = "review"
            self._review_callback = on_complete
            self.question_header.update(f"{title}\n{summary}")
            self.question_header.display = True
            self.review_details.update(details)
            self.review_details.display = bool(details)
            initial_index = next(
                (index for index, choice in enumerate(choices) if choice.id == initial_choice_id),
                0,
            )
            lists = [InlineChoiceList(choices, initial_index=initial_index)]
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
        self.choice_panel.display = True
        self.choice_panel.mount(*lists)

    def _handle_choice_selected(self, event: ListView.Selected) -> None:
        choice_list = event.list_view
        if not isinstance(choice_list, InlineChoiceList) or choice_list not in self._choice_lists:
            return
        row = event.item
        if not isinstance(row, ChoiceRow):
            return
        self._activate_choice(choice_list, row)

    def _handle_choice_input_submitted(self, event: Input.Submitted) -> None:
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
        self.call_after_refresh(self.input.focus)

    def _move_question(self, step: int) -> None:
        target = self._question_index + step
        if 0 <= target < len(self._questions):
            self._show_question(target)

    def _active_choice_list(self) -> InlineChoiceList:
        index = self._question_index if self.questionnaire_active else 0
        return self._choice_lists[index]

    def handle_choice_input_key(self, key: str) -> bool:
        """Route terminal-input keys to the embedded choice prompt when active."""

        if self._choice_kind is None:
            return False
        if key == "up":
            self._active_choice_list().move_highlight(-1)
            return True
        if key == "down":
            self._active_choice_list().move_highlight(1)
            return True
        if key == "left":
            if self.questionnaire_active:
                self._move_question(-1)
            return True
        if key == "right":
            if self.questionnaire_active:
                self._move_question(1)
            return True
        if key == "enter":
            row = self._active_choice_list().highlighted_row
            if row is not None:
                self._activate_choice(self._active_choice_list(), row)
            return True
        return False

    def _activate_choice(self, choice_list: InlineChoiceList, row: ChoiceRow) -> None:
        if row.choice.custom:
            row.begin_edit(self._existing_custom_answer(choice_list))
            return
        self._accept_choice(choice_list, row, None)

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
        answers = self._question_answers.get(question.id, [])
        return answers[0] if answers else ""

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
        if row.choice.custom:
            answers = [custom_value] if custom_value is not None else []
        else:
            answers = [row.choice.label]
        self._question_answers[question.id] = answers
        self._question_selections[question.id] = row.choice.id
        for candidate in choice_list.rows:
            candidate.set_class(candidate is row, "-selected-answer")

        unanswered = [index for index, item in enumerate(self._questions) if item.id not in self._question_answers]
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
        self.review_details.display = False
        self.choice_panel.display = False
        self.input.focus()
