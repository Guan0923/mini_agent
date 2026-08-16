"""LLM planner formats behavior."""

from __future__ import annotations

from typing import Any

from backend.runtime.core.context import PreparedResponse

from ..model_outputs import parse_json_object


class FormatMixin:
    @staticmethod
    def _response_diagnostics(prepared: PreparedResponse) -> dict[str, str | int | None]:
        return {
            "finish_reason": prepared.finish_reason,
            "content_chars": len(prepared.message.content or ""),
            "reasoning_chars": len(prepared.message.reasoning or ""),
        }

    @staticmethod
    def _json_object(raw: str, operation: str | None = None) -> dict[str, Any]:
        return parse_json_object(raw, operation)
