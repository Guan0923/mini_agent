"""Selectable scroll container with right-click clipboard support."""

from __future__ import annotations

from textual import events
from textual.containers import VerticalScroll


class CopyableScroll(VerticalScroll):
    """Expose child text to Textual selection and copy it on right-click."""

    def on_mouse_down(self, event: events.MouseDown) -> None:
        if event.button != 3:
            return
        selected = self.screen.get_selected_text()
        if selected:
            self.app.copy_to_clipboard(selected)
            self.screen.clear_selection()
        event.prevent_default()
        event.stop()
