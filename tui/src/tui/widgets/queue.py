"""Compact presentation for user messages waiting to run."""

from __future__ import annotations

from textual.widgets import Static


class QueuedMessages(Static):
    """Show pending messages without making them look like sent conversation turns."""

    def __init__(self) -> None:
        self.messages: list[str] = []
        super().__init__(id="queued-messages", markup=False)
        self.display = False

    def set_messages(self, messages: list[str]) -> None:
        self.messages = list(messages)
        self.display = bool(self.messages)
        if self.messages:
            entries = "\n".join(f"{index}. {message}" for index, message in enumerate(self.messages, start=1))
            self.update(f"QUEUE · {len(self.messages)} waiting\n{entries}")
        else:
            self.update("")
