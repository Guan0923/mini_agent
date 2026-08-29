"""Human-readable summaries for tool approval requests."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from backend.runtime.core.contracts import InterruptRequest

_MISSING = "<missing>"
_MARKDOWN_SPECIAL = re.compile(r"([\\`*_{}\[\]()<>#+\-.!|])")
_MAX_TARGET_CHARS = 120


@dataclass(frozen=True)
class ToolReviewSummary:
    """One concise tool approval summary with plain and Markdown renderers."""

    tool: str
    target: str | None = None

    def plain(self) -> str:
        lines = [f"Tool: {self.tool}"]
        if self.target is not None:
            lines.append(f"Target: {self.target}")
        return "\n".join(lines)

    def markdown(self) -> str:
        sections = [f"**Tool:** {_escape_markdown(self.tool)}"]
        if self.target is not None:
            sections.append(f"**Target:** {_escape_markdown(self.target)}")
        return "\n\n".join(sections)


def format_tool_review(request: InterruptRequest) -> ToolReviewSummary:
    """Describe a tool call by its name and primary target only."""

    tool = str(request.data.get("tool") or "unknown")
    raw_arguments = request.data.get("arguments")
    arguments = raw_arguments if isinstance(raw_arguments, dict) else {}
    target_field = {
        "run_command": "command",
        "write_file": "path",
        "edit_file": "path",
        "web_search": "query",
        "web_fetch": "url",
    }.get(tool)
    return ToolReviewSummary(tool, _target(arguments.get(target_field, _MISSING)) if target_field else None)


def _target(value: Any) -> str:
    text = value if isinstance(value, str) else _fallback_value(value)
    text = " ".join(text.split()) or _MISSING
    return text if len(text) <= _MAX_TARGET_CHARS else f"{text[: _MAX_TARGET_CHARS - 3]}..."


def _fallback_value(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, dict):
        return f"<object with {len(value)} fields>"
    if isinstance(value, (list, tuple)):
        return f"<list with {len(value)} items>"
    return str(value)


def _escape_markdown(value: str) -> str:
    return _MARKDOWN_SPECIAL.sub(r"\\\1", value)
