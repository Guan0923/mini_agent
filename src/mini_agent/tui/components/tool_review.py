"""Human-readable summaries for tool approval requests."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from mini_agent.runtime.core.contracts import InterruptRequest

_MISSING = "<missing>"
_MARKDOWN_SPECIAL = re.compile(r"([\\`*_{}\[\]()<>#+\-.!|])")
_MAX_FALLBACK_TEXT_CHARS = 120


@dataclass(frozen=True)
class ToolReviewSummary:
    """One semantic tool approval summary with plain and Markdown renderers."""

    tool: str
    action: str
    detail_label: str | None = None
    detail: str | None = None

    def plain(self) -> str:
        lines = [f"Tool: {self.tool}", f"Action: {self.action}"]
        if self.detail_label is not None and self.detail is not None:
            if "\n" in self.detail:
                lines.extend((f"{self.detail_label}:", self.detail))
            else:
                lines.append(f"{self.detail_label}: {self.detail}")
        return "\n".join(lines)

    def markdown(self) -> str:
        sections = [
            f"**Tool:** {_escape_markdown(self.tool)}",
            f"**Action:** {_escape_markdown(self.action)}",
        ]
        if self.detail_label is not None and self.detail is not None:
            sections.append(f"**{_escape_markdown(self.detail_label)}:**\n\n{_indented_code(self.detail)}")
        return "\n\n".join(sections)


def format_tool_review(request: InterruptRequest) -> ToolReviewSummary:
    """Describe the requested operation without exposing raw argument JSON."""

    tool = str(request.data.get("tool") or "unknown")
    raw_arguments = request.data.get("arguments")
    arguments = raw_arguments if isinstance(raw_arguments, dict) else {}

    if tool == "run_command":
        return ToolReviewSummary(tool, "Run command", "Command", _field(arguments, "command"))
    if tool == "write_file":
        action = "Create or overwrite file" if arguments.get("overwrite") is True else "Create file"
        return ToolReviewSummary(tool, action, "Path", _field(arguments, "path"))
    if tool == "edit_file":
        return ToolReviewSummary(tool, "Edit file", "Path", _field(arguments, "path"))
    if tool == "web_search":
        return ToolReviewSummary(tool, "Search the web", "Query", _field(arguments, "query"))
    if tool == "web_fetch":
        return ToolReviewSummary(tool, "Fetch web content", "URL", _field(arguments, "url"))

    if not arguments:
        return ToolReviewSummary(tool, "Call tool")
    parameters = "\n".join(
        f"{key}: {_fallback_value(value)}" for key, value in sorted(arguments.items(), key=lambda item: str(item[0]))
    )
    return ToolReviewSummary(tool, "Call tool", "Parameters", parameters)


def _field(arguments: dict[str, Any], name: str) -> str:
    value = arguments.get(name, _MISSING)
    if isinstance(value, str):
        return value or _MISSING
    return _fallback_value(value)


def _fallback_value(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, dict):
        return f"<object with {len(value)} fields>"
    if isinstance(value, (list, tuple)):
        return f"<list with {len(value)} items>"
    text = str(value)
    if "\n" in text or "\r" in text:
        return f"<multiline text, {len(text)} characters>"
    if len(text) > _MAX_FALLBACK_TEXT_CHARS:
        return f"<text, {len(text)} characters>"
    return text


def _escape_markdown(value: str) -> str:
    return _MARKDOWN_SPECIAL.sub(r"\\\1", value)


def _indented_code(value: str) -> str:
    lines = value.splitlines() or [_MISSING]
    return "\n".join(f"    {line}" for line in lines)
