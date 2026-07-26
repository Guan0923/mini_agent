"""Nested Markdown transcript widgets used by the terminal view."""

from __future__ import annotations

from collections.abc import Callable, Iterable

from textual import events
from textual.containers import VerticalScroll
from textual.timer import Timer
from textual.widgets import Collapsible, Markdown, Static
from textual.widgets.text_area import Selection

from .transcript_content import (
    SPINNER_FRAMES,
    CompactProgress,
    MarkdownBody,
    ProcessingProgress,
    StatusLeaf,
)

__all__ = [
    "CompactProgress",
    "MarkdownBody",
    "ProcessingProgress",
    "StatusLeaf",
    "TranscriptNode",
    "TranscriptScroll",
]


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
        # Keep the child in Collapsible's compose list for the not-yet-composed
        # case, and also track it for the already-composed-but-not-mounted case.
        self._contents_list.append(node)
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
        pending = [node for node in self._pending_nodes if not node.is_attached]
        self._pending_nodes = []
        if pending:
            contents.mount(*pending)


class TranscriptScroll(VerticalScroll):
    """The transcript container, with small compatibility helpers for old callers."""

    def __init__(self) -> None:
        super().__init__(id="transcript", classes="transcript-scroll")
        self.soft_wrap = True
        self.read_only = True
        self._plain_text = ""
        self._text_source: Callable[[], tuple[int, str]] | None = None
        self._source_revision = -1
        self.selection = Selection.cursor((0, 0))
        self._pending_top_levels: list[TranscriptNode] = []

    @property
    def text(self) -> str:
        self._refresh_text_source()
        return self._plain_text

    @property
    def selected_text(self) -> str:
        return self.text if not self.selection.is_empty else ""

    def set_text_source(self, source: Callable[[], tuple[int, str]]) -> None:
        """Use a revisioned source while preserving legacy append behavior."""

        self._text_source = source
        self._source_revision = -1

    def append_text(self, text: str) -> None:
        self._plain_text = f"{self.text}{text}"

    def load_text(self, text: str) -> None:
        self._plain_text = text
        self.selection = Selection.cursor((0, 0))

    def sync_text(self, text: str) -> None:
        """Refresh the compatibility/copy mirror without changing selection."""
        self._plain_text = text

    def select_all(self) -> None:
        text = self.text
        line_count = max(1, text.count("\n") + 1)
        last_column = len(text.rsplit("\n", 1)[-1])
        self.selection = Selection((0, 0), (line_count - 1, last_column))
        if self.is_mounted:
            self.call_after_refresh(self._notify_selection)

    def _refresh_text_source(self) -> None:
        if self._text_source is None:
            return
        revision, text = self._text_source()
        if revision != self._source_revision:
            self._plain_text = text
            self._source_revision = revision

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
            return
        callback = getattr(self.app, "focus_input_if_no_selection", None)
        if callback is not None:
            callback()

    def on_mouse_up(self, event: events.MouseUp) -> None:
        """Check Textual's screen selection after a transcript drag completes."""

        self.call_after_refresh(self._notify_selection)

    def _notify_selection(self) -> None:
        callback = getattr(self.app, "blur_input_for_transcript_selection", None)
        if callback is not None:
            callback()

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
