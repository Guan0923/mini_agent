"""Status-line widgets."""

from rich.text import Text
from textual.widgets import Static


class ContextProgress(Static):
    """One-line context usage meter with the compression threshold marked."""

    DEFAULT_CSS = "ContextProgress { width: 1fr; min-width: 1; height: 1; padding: 0 1; background: #263442; }"

    def __init__(self) -> None:
        super().__init__(id="context-progress")
        self.estimated_tokens: int | None = None
        self.context_size: int | None = None
        self.threshold = 0.8

    @property
    def ratio(self) -> float | None:
        if self.estimated_tokens is None or not self.context_size:
            return None
        return self.estimated_tokens / self.context_size

    def set_usage(
        self,
        estimated_tokens: int | None,
        context_size: int | None,
        threshold: float = 0.8,
    ) -> None:
        self.estimated_tokens = estimated_tokens
        self.context_size = context_size
        self.threshold = max(0.0, min(threshold, 1.0))
        self.refresh()

    def render(self) -> Text:
        width = max(1, self.size.width - 2)
        ratio = self.ratio
        if ratio is None:
            detailed = "CONTEXT N/A "
            compact = "CTX N/A "
        else:
            percent = ratio * 100
            detailed = f"CONTEXT {self.estimated_tokens:,} / {self.context_size:,} {percent:.0f}% "
            compact = f"CTX {percent:.0f}% "
        if width - len(detailed) >= 8:
            label = detailed
        elif width - len(compact) >= 4:
            label = compact
        elif width >= 8:
            label = "CTX "
        else:
            label = ""
        bar_width = max(1, width - len(label))
        marker = min(bar_width - 1, max(0, round(self.threshold * (bar_width - 1))))
        filled = 0 if ratio is None else min(bar_width, max(0, round(ratio * bar_width)))
        bar = ["━" if index < filled else "─" for index in range(bar_width)]
        bar[marker] = "┊"
        text = Text(label + "".join(bar), no_wrap=True, overflow="crop")
        text.stylize("#8a96a3", 0, len(label))
        fill_color = "#65b8a6"
        if ratio is not None and ratio >= 1:
            fill_color = "#e26464"
        elif ratio is not None and ratio >= self.threshold:
            fill_color = "#e3b65f"
        text.stylize("#47515b", len(label), len(text))
        for index in range(filled):
            if index != marker:
                text.stylize(fill_color, len(label) + index, len(label) + index + 1)
        text.stylize("#d7dde5 bold", len(label) + marker, len(label) + marker + 1)
        return text
