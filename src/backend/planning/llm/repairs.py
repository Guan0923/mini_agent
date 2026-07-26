"""LLM planner repairs behavior."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from backend.domain import (
    AssistantMessage,
    ModelOutputError,
    UserMessage,
)
from backend.runtime.core.context import AgentRuntime


class RepairMixin:
    def consume_output_repairs(self) -> list[dict[str, str | int]]:
        repairs = self._output_repairs
        self._output_repairs = []
        return repairs

    def _with_output_repair(
        self,
        runtime: AgentRuntime,
        operation: str,
        request: Callable[[UserMessage | None], Any],
    ) -> Any:
        self._output_repairs.clear()
        correction: UserMessage | None = None
        repairs: list[dict[str, str | int]] = []
        max_repairs = runtime.state.runner_settings.max_model_repairs
        for attempt in range(max_repairs + 1):
            try:
                result = request(correction)
            except ModelOutputError as exc:
                repair: dict[str, str | int] = {
                    "phase": operation,
                    "attempt": attempt + 1,
                    "validation_error": exc.validation_error,
                    "invalid_output_preview": exc.invalid_output_preview,
                    "outcome": "retrying",
                }
                repairs.append(repair)
                if attempt >= max_repairs:
                    for item in repairs:
                        item["outcome"] = "failed"
                    self._output_repairs.extend(repairs)
                    raise
                correction = UserMessage(content=self._repair_instruction(exc))
                continue
            for item in repairs:
                item["outcome"] = "repaired"
            self._output_repairs.extend(repairs)
            return result
        raise AssertionError("Model output repair loop ended without an outcome.")

    @staticmethod
    def _repair_instruction(error: ModelOutputError) -> str:
        preview = error.invalid_output_preview.strip()
        invalid = f"\n\nInvalid output:\n{preview}" if preview else ""
        return (
            "[Model output correction]\n"
            "Your previous response could not be executed.\n\n"
            f"Validation error: {error.validation_error}."
            f"{invalid}\n\n"
            "Return the complete response again using the required schema. "
            "Do not explain the correction."
        )

    @staticmethod
    def _message_preview(message: AssistantMessage) -> str:
        return json.dumps(
            {
                "content": message.content,
                "tool_calls": [{"name": tool.name, "arguments": tool.arguments} for tool in message.tool_messages],
            },
            ensure_ascii=False,
            default=str,
        )
