"""Read-only paged conversation history."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widget import Widget
from textual.widgets import Static

from ..latex import LatexMarkdown
from ..selectable import CopyableScroll

HistoryPageLoader = Callable[[int | None], tuple[list[dict[str, str]], int | None]]


class HistoryScreen(Screen[None]):
    """Show a bounded transcript and load older pages only on request."""

    CSS = """
    HistoryScreen { background: #101418; color: #d7dde5; }
    #history-header {
        height: 1;
        padding: 0 1;
        background: #263442;
        color: #9fc3e8;
    }
    #history-log {
        height: 1fr;
        padding: 0 1;
        background: #101418;
        scrollbar-color: #5f6b76;
    }
    .history-role {
        height: auto;
        color: #9fc3e8;
        text-style: bold;
    }
    .history-message {
        height: auto;
        padding: 0 0 0 2;
        margin-bottom: 1;
    }
    #history-footer {
        height: 1;
        padding: 0 1;
        background: #263442;
        color: #9fc3e8;
    }
    """

    BINDINGS = [
        Binding("escape", "close", show=False, priority=True),
        Binding("pageup", "page_up", show=False, priority=True),
        Binding("pagedown", "page_down", show=False, priority=True),
        Binding("home", "home", show=False, priority=True),
        Binding("end", "end", show=False, priority=True),
        Binding("ctrl+home", "load_older", show=False, priority=True),
    ]

    def __init__(
        self,
        session_label: str,
        messages: list[dict[str, str]],
        *,
        before_id: int | None = None,
        load_older: HistoryPageLoader | None = None,
    ) -> None:
        super().__init__()
        self.session_label = session_label
        self.messages = messages
        self._before_id = before_id
        self._load_older = load_older
        self.history_log = CopyableScroll(*self._content_widgets(), id="history-log")

    def _content_widgets(self) -> list[Widget]:
        if not self.messages:
            return [Static("No conversation history.", classes="history-message")]
        widgets: list[Widget] = []
        for message in self.messages:
            role = message.get("role", "system").upper()
            widgets.append(Static(role, classes="history-role"))
            widgets.append(LatexMarkdown(message.get("content", ""), classes="history-message"))
        return widgets

    def compose(self) -> ComposeResult:
        yield Static(f"HISTORY | {self.session_label}", id="history-header")
        yield self.history_log
        yield Static("Ctrl+Home loads older messages | Esc returns", id="history-footer")

    def on_mount(self) -> None:
        self.call_after_refresh(self.history_log.scroll_end, animate=False)

    def action_close(self) -> None:
        self.dismiss()

    def action_page_up(self) -> None:
        self.history_log.scroll_page_up(animate=False)

    def action_page_down(self) -> None:
        self.history_log.scroll_page_down(animate=False)

    def action_home(self) -> None:
        self.history_log.scroll_home(animate=False)

    def action_end(self) -> None:
        self.history_log.scroll_end(animate=False)

    async def action_load_older(self) -> None:
        """Fetch and prepend one older page without blocking the Textual loop."""

        if self._load_older is None or self._before_id is None:
            return
        messages, before_id = await asyncio.to_thread(self._load_older, self._before_id)
        if not messages:
            self._before_id = None
            return
        self.messages = [*messages, *self.messages]
        self._before_id = before_id
        await self.history_log.remove_children()
        await self.history_log.mount(*self._content_widgets())
        self.history_log.scroll_home(animate=False)
