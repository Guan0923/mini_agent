"""Inline choice widgets for questionnaires and approval prompts."""

from __future__ import annotations

from dataclasses import dataclass

from textual.widgets import Input, ListItem, ListView, Static


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

    def __init__(
        self,
        items: tuple[ChoiceItem, ...],
        *,
        question_index: int | None = None,
        initial_index: int = 0,
    ) -> None:
        self.question_index = question_index
        self.rows = tuple(ChoiceRow(item) for item in items)
        super().__init__(*self.rows, initial_index=initial_index, classes="choice-list")

    @property
    def highlighted_row(self) -> ChoiceRow | None:
        child = self.highlighted_child
        return child if isinstance(child, ChoiceRow) else None

    def watch_index(self, index: int | None) -> None:
        for row_index, row in enumerate(self.rows):
            row.set_class(row_index == index, "-highlighted-choice")

    def move_highlight(self, step: int) -> None:
        """Move the highlighted row without wrapping at either end."""

        current = self.index if self.index is not None else 0
        self.index = max(0, min(len(self.rows) - 1, current + step))
