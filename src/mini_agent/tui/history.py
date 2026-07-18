"""Read-only full-screen conversation history."""

from __future__ import annotations

from rich.markdown import Markdown
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import RichLog, Static


class HistoryScreen(Screen[None]):
    """Show the current session transcript without exposing message input."""

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
    ]

    def __init__(self, session_label: str, messages: list[dict[str, str]]) -> None:
        super().__init__()
        self.session_label = session_label
        self.messages = messages
        self.history_log = RichLog(wrap=True, markup=False, auto_scroll=False, id="history-log")

    def compose(self) -> ComposeResult:
        yield Static(f"HISTORY | {self.session_label}", id="history-header")
        yield self.history_log
        yield Static("READ ONLY | Esc to return", id="history-footer")

    def on_mount(self) -> None:
        if not self.messages:
            self.history_log.write(Text("No conversation history.", style="dim"))
        else:
            for message in self.messages:
                role = message.get("role", "system").upper()
                self.history_log.write(Text(role, style="bold #9fc3e8"))
                self.history_log.write(Markdown(message.get("content", "")))
                self.history_log.write("")
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