"""Read-only full-screen session and trace views."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import Static

from ..selectable import CopyableScroll


class _InspectionScreen(Screen[None]):
    """Show command output without replacing the live transcript."""

    CSS = """
    SessionsScreen, TraceScreen { background: #101418; color: #d7dde5; }
    #inspection-header {
        height: 1;
        padding: 0 1;
        background: #263442;
        color: #9fc3e8;
    }
    #inspection-log {
        height: 1fr;
        padding: 0 1;
        background: #101418;
        scrollbar-color: #5f6b76;
    }
    .inspection-content {
        height: auto;
    }
    #inspection-footer {
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

    def __init__(self, heading: str, content: list[str]) -> None:
        super().__init__()
        self.heading = heading
        self.content = content
        items = [Static(item, markup=False, classes="inspection-content") for item in content]
        self.content_log = CopyableScroll(*items, id="inspection-log")

    def compose(self) -> ComposeResult:
        yield Static(self.heading, id="inspection-header")
        yield self.content_log
        yield Static("READ ONLY | Select text and right-click to copy | Esc to return", id="inspection-footer")

    def on_mount(self) -> None:
        self.call_after_refresh(self.content_log.scroll_end, animate=False)

    def action_close(self) -> None:
        self.dismiss()

    def action_page_up(self) -> None:
        self.content_log.scroll_page_up(animate=False)

    def action_page_down(self) -> None:
        self.content_log.scroll_page_down(animate=False)

    def action_home(self) -> None:
        self.content_log.scroll_home(animate=False)

    def action_end(self) -> None:
        self.content_log.scroll_end(animate=False)


class SessionsScreen(_InspectionScreen):
    """Show saved sessions in a read-only full-screen list."""

    def __init__(self, sessions: list[str]) -> None:
        super().__init__("SESSIONS", sessions or ["No saved sessions."])


class TraceScreen(_InspectionScreen):
    """Show the last run trace as complete formatted JSON."""

    def __init__(self, run_label: str, trace: str) -> None:
        super().__init__(f"TRACE | {run_label}", [trace])
