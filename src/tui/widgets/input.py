"""Transcript and command input widgets."""

from __future__ import annotations

from textual import events
from textual.message import Message
from textual.widgets import TextArea
from textual.widgets.text_area import Selection


class TranscriptTextArea(TextArea):
    """Read-only transcript area with copy and follow-tail callbacks."""

    def on_mouse_down(self, event: events.MouseDown) -> None:
        if event.button == 3:
            copy_selection = getattr(self.app, "copy_transcript_selection", None)
            if callable(copy_selection):
                copy_selection()
            event.prevent_default()
            event.stop()

    def _on_mouse_scroll_up(self, event: events.MouseScrollUp) -> None:
        pause_following = getattr(self.app, "pause_following", None)
        if callable(pause_following):
            pause_following()
        super()._on_mouse_scroll_up(event)

    def _on_mouse_scroll_down(self, event: events.MouseScrollDown) -> None:
        super()._on_mouse_scroll_down(event)
        resume_following = getattr(self.app, "_resume_follow_if_at_end", None)
        if callable(resume_following):
            self.app.call_after_refresh(resume_following)


class TerminalInput(TextArea):
    """Multiline editor that submits on Enter and inserts newlines on Ctrl+J."""

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
