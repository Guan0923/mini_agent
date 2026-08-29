"""Leaf widgets used inside structured transcript containers."""

from __future__ import annotations

from collections.abc import Callable

from textual.timer import Timer
from textual.widgets import Static

from ..latex import LatexMarkdown

SPINNER_FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")


class CompactProgress(Static):
    """Indeterminate progress shown while context compaction is running."""

    BAR_WIDTH = 18
    BLOCK_WIDTH = 5

    def __init__(self) -> None:
        self._offset = 0
        self._animation_timer: Timer | None = None
        self.running = True
        super().__init__(self._running_text(), markup=False, classes="compact-progress")

    def on_mount(self) -> None:
        if self.running and self._animation_timer is None:
            self._animation_timer = self.set_interval(0.12, self._tick)

    def on_unmount(self) -> None:
        self.stop()

    def _tick(self) -> None:
        if not self.running:
            return
        self._offset = (self._offset + 1) % (self.BAR_WIDTH + self.BLOCK_WIDTH)
        self.update(self._running_text())

    def _running_text(self) -> str:
        start = self._offset - self.BLOCK_WIDTH + 1
        cells = ["█" if start <= index <= self._offset else " " for index in range(self.BAR_WIDTH)]
        return f"正在 compact 中  [{''.join(cells)}]"

    def complete(self, previous_messages: int, remaining_messages: int) -> None:
        self.stop()
        self.update(f"compact 完成  [{'█' * self.BAR_WIDTH}]  {previous_messages} → {remaining_messages} 条消息")

    def no_op(self) -> None:
        self.stop()
        self.update(f"无需 compact  [{'─' * self.BAR_WIDTH}]  没有可压缩的旧对话")

    def fail(self, message: str) -> None:
        self.stop()
        detail = " ".join(message.split())[:200]
        self.update(f"compact 失败  [{'!' * self.BAR_WIDTH}]  {detail}")

    def stop(self) -> None:
        self.running = False
        if self._animation_timer is not None:
            self._animation_timer.stop()
            self._animation_timer = None


class ProcessingProgress(Static):
    """A concise activity indicator used by minimal transcript rendering."""

    def __init__(self) -> None:
        self._spinner_index = 0
        self._animation_timer: Timer | None = None
        self.running = True
        super().__init__(self._running_text(), markup=False, classes="processing-progress")

    def on_mount(self) -> None:
        if self.running and self._animation_timer is None:
            self._animation_timer = self.set_interval(0.12, self._tick)

    def on_unmount(self) -> None:
        self.stop()

    def _tick(self) -> None:
        if not self.running:
            return
        self._spinner_index = (self._spinner_index + 1) % len(SPINNER_FRAMES)
        self.update(self._running_text())

    def _running_text(self) -> str:
        return f"正在处理中 {SPINNER_FRAMES[self._spinner_index]}"

    def complete(self) -> None:
        self.stop()
        self.update("处理完成")

    def fail(self, message: str) -> None:
        self.stop()
        detail = " ".join(message.split())[:160]
        self.update(f"处理失败: {detail}" if detail else "处理失败")

    def stop(self) -> None:
        self.running = False
        if self._animation_timer is not None:
            self._animation_timer.stop()
            self._animation_timer = None


class MarkdownBody(LatexMarkdown):
    """A Markdown widget with a synchronous source cache for streaming text."""

    def __init__(self, markdown: str = "", **kwargs: object) -> None:
        self.markdown_text = markdown
        self._revision = 0
        self._render_scheduled = False
        self._render_running = False
        self._source_listener: Callable[[MarkdownBody, str, str], None] | None = None
        super().__init__(markdown, **kwargs)

    def set_source_listener(self, listener: Callable[[MarkdownBody, str, str], None] | None) -> None:
        self._source_listener = listener

    def set_markdown(self, markdown: str) -> None:
        """Cache the latest source and render it at most once per refresh."""

        previous = self.markdown_text
        self.markdown_text = markdown
        if previous != markdown and self._source_listener is not None:
            self._source_listener(self, previous, markdown)
        self._revision += 1
        if not self.is_mounted:
            self._initial_markdown = markdown
            return
        if not self._render_scheduled:
            self._render_scheduled = True
            self.call_after_refresh(self._flush_render)

    def append_markdown(self, delta: str) -> None:
        self.set_markdown(f"{self.markdown_text}{delta}")

    def _flush_render(self) -> None:
        self._render_scheduled = False
        if not self.is_mounted:
            self._initial_markdown = self.markdown_text
            return
        if self._render_running:
            return
        self._render_running = True
        self.run_worker(self._render_latest)

    async def _render_latest(self) -> None:
        """Serialize Markdown updates and catch up to the newest revision."""

        try:
            while self.is_mounted:
                revision = self._revision
                markdown = self.markdown_text
                await self.update(markdown)
                if revision == self._revision:
                    return
        finally:
            self._render_running = False


class StatusLeaf(Static):
    """A concise, non-expandable tool status."""

    def __init__(self, status: str = "pending") -> None:
        self.status = status
        super().__init__(f"status: {status}", markup=False, classes="transcript-status")

    def set_status(self, status: str) -> None:
        self.status = status
        self.update(f"status: {status}")
