"""Nested Markdown transcript widgets used by the terminal view."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable

from textual import events
from textual.await_complete import AwaitComplete
from textual.containers import VerticalScroll
from textual.timer import Timer
from textual.widgets import Collapsible, Markdown, Static
from textual.widgets.text_area import Selection


SPINNER_FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")


class MarkdownBody(Markdown):
    """A Markdown widget with a synchronous source cache for streaming text."""

    def __init__(self, markdown: str = "", **kwargs: object) -> None:
        self.markdown_text = markdown
        self._revision = 0
        super().__init__(markdown, **kwargs)

    def set_markdown(self, markdown: str) -> None:
        """Replace content while keeping only the latest asynchronous render."""
        self.markdown_text = markdown
        self._revision += 1
        if self.is_mounted:
            self.run_worker(self._render_stream(self._revision, markdown), exclusive=True)
        else:
            self._initial_markdown = markdown

    def append_markdown(self, delta: str) -> None:
        self.set_markdown(f"{self.markdown_text}{delta}")

    def update(self, markdown: str) -> AwaitComplete:
        transcript = next(
            (ancestor for ancestor in self.ancestors if isinstance(ancestor, TranscriptScroll)),
            None,
        )
        completed = super().update(markdown)

        async def restore_scroll() -> None:
            await completed
            if transcript is None:
                return
            if getattr(self.app, "follow_tail", True):
                settle = getattr(self.app, "_settle_follow_latest", None)
                if settle is not None:
                    self.call_after_refresh(settle)
                else:
                    self.call_after_refresh(transcript.scroll_end, animate=False)
            else:
                target = getattr(self.app, "_paused_scroll_y", transcript.scroll_y)
                self.call_after_refresh(transcript.scroll_to, y=target, animate=False)

        return AwaitComplete(restore_scroll())

    async def _render_stream(self, revision: int, markdown: str) -> None:
        if revision != self._revision:
            return
        await self.update(markdown)


class StatusLeaf(Static):
    """A concise, non-expandable tool status."""

    def __init__(self, status: str = "pending") -> None:
        self.status = status
        super().__init__(f"status: {status}", markup=False, classes="transcript-status")

    def set_status(self, status: str) -> None:
        self.status = status
        self.update(f"status: {status}")


class TranscriptNode(Collapsible):
    """A titled transcript branch which may show activity while collapsed."""

    def __init__(
        self,
        title: str,
        *children: Static | Markdown | Collapsible,
        collapsed: bool = False,
        classes: str | None = None,
    ) -> None:
        self.title_text = title
        self.activity = False
        self._spinner_index = 0
        self._activity_timer: Timer | None = None
        self._pending_nodes: list[Static | Markdown | Collapsible] = []
        super().__init__(
            *children,
            title=title,
            collapsed=collapsed,
            collapsed_symbol="▶",
            expanded_symbol="▼",
            classes=classes or "transcript-node",
        )

    @property
    def display_title(self) -> str:
        if self.activity and self.collapsed:
            return f"{SPINNER_FRAMES[self._spinner_index]} {self.title_text}"
        return f"{'▶' if self.collapsed else '▼'} {self.title_text}"

    def _watch_collapsed(self, collapsed: bool) -> None:
        super()._watch_collapsed(collapsed)
        self._sync_activity_timer()
        self._sync_title()

    def set_activity(self, active: bool) -> None:
        """Start or stop the collapsed-node activity indicator."""
        if self.activity == active:
            return
        self.activity = active
        self._sync_activity_timer()
        self._sync_title()

    def _on_mount(self, event: events.Mount) -> None:
        super()._on_mount(event)
        self._sync_activity_timer()
        self._sync_title()
        if self._pending_nodes:
            self.call_after_refresh(self._flush_pending_nodes)

    def _sync_activity_timer(self) -> None:
        should_run = self.activity and self.collapsed and self.is_mounted
        if should_run and self._activity_timer is None:
            self._activity_timer = self.set_interval(0.12, self._tick_activity)
        elif not should_run and self._activity_timer is not None:
            self._activity_timer.stop()
            self._activity_timer = None

    def _tick_activity(self) -> None:
        if not self.activity:
            return
        self._spinner_index = (self._spinner_index + 1) % len(SPINNER_FRAMES)
        self._sync_title()

    def _sync_title(self) -> None:
        if self.activity and self.collapsed:
            self._title.collapsed_symbol = SPINNER_FRAMES[self._spinner_index]
        else:
            self._title.collapsed_symbol = "▶"
        self._title.expanded_symbol = "▼"
        self.title = self.title_text
        self._title._update_label()

    def add_node(self, node: Static | Markdown | Collapsible) -> None:
        """Append a child, deferring dynamic mount until Contents is attached."""
        contents = next(iter(self.query(Collapsible.Contents)), None) if self.is_attached else None
        if contents is not None and contents.is_attached:
            contents.mount(node)
            return
        self._pending_nodes.append(node)
        if self.is_attached:
            self.call_after_refresh(self._flush_pending_nodes)

    def _flush_pending_nodes(self) -> None:
        if not self._pending_nodes or not self.is_attached:
            return
        contents = next(iter(self.query(Collapsible.Contents)), None)
        if contents is None or not contents.is_attached:
            self.call_after_refresh(self._flush_pending_nodes)
            return
        pending = self._pending_nodes
        self._pending_nodes = []
        contents.mount(*pending)


class TranscriptScroll(VerticalScroll):
    """The transcript container, with small compatibility helpers for old callers."""

    def __init__(self) -> None:
        super().__init__(id="transcript", classes="transcript-scroll")
        self.soft_wrap = True
        self.read_only = True
        self._plain_text = ""
        self.selection = Selection.cursor((0, 0))
        self._pending_top_levels: list[TranscriptNode] = []

    @property
    def text(self) -> str:
        return self._plain_text

    @property
    def selected_text(self) -> str:
        return self._plain_text if not self.selection.is_empty else ""

    def append_text(self, text: str) -> None:
        self._plain_text = f"{self._plain_text}{text}"

    def load_text(self, text: str) -> None:
        self._plain_text = text
        self.selection = Selection.cursor((0, 0))

    def sync_text(self, text: str) -> None:
        """Refresh the compatibility/copy mirror without changing selection."""
        self._plain_text = text

    def select_all(self) -> None:
        line_count = max(1, self._plain_text.count("\n") + 1)
        last_column = len(self._plain_text.rsplit("\n", 1)[-1])
        self.selection = Selection((0, 0), (line_count - 1, last_column))

    def add_top_level(self, node: TranscriptNode) -> None:
        if self.is_attached:
            self.mount(node)
        else:
            self._pending_top_levels.append(node)

    def _on_mount(self, event: events.Mount) -> None:
        super()._on_mount(event)
        if self._pending_top_levels:
            self.call_after_refresh(self._flush_pending_top_levels)

    def _flush_pending_top_levels(self) -> None:
        if not self.is_attached or not self._pending_top_levels:
            return
        pending = self._pending_top_levels
        self._pending_top_levels = []
        self.mount(*pending)

    def on_mouse_down(self, event: events.MouseDown) -> None:
        if event.button == 3:
            callback = getattr(self.app, "copy_transcript_selection", None)
            if callback is not None:
                callback()
            event.prevent_default()
            event.stop()

    def _on_mouse_scroll_up(self, event: events.MouseScrollUp) -> None:
        callback = getattr(self.app, "pause_following", None)
        if callback is not None:
            callback()
        super()._on_mouse_scroll_up(event)
        remember = getattr(self.app, "_remember_paused_scroll", None)
        if remember is not None:
            self.call_after_refresh(remember)

    def _on_mouse_scroll_down(self, event: events.MouseScrollDown) -> None:
        super()._on_mouse_scroll_down(event)
        callback = getattr(self.app, "_resume_follow_if_at_end", None)
        if callback is not None:
            self.call_after_refresh(callback)

    def clear_nodes(self, nodes: Iterable[TranscriptNode]) -> None:
        """Detach rendered nodes; callers clear their indexing state separately."""
        pending = set(nodes)
        self._pending_top_levels = [node for node in self._pending_top_levels if node not in pending]
        for node in nodes:
            if node.is_mounted:
                node.remove()
