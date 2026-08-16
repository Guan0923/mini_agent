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
        attempt = 0
        while True:
            cancel_requested = runtime.services.cancel_requested
            if cancel_requested is not None and cancel_requested():
                for item in repairs:
                    item["outcome"] = "cancelled"
                self._output_repairs.extend(repairs)
                raise ModelOutputError(
                    "Model output repair cancelled by user.",
                    operation=operation,
                    diagnostics={"cancelled": True},
                )
            attempt += 1
            try:
                result = request(correction)
            except ModelOutputError as exc:
                repair: dict[str, str | int] = {
                    "phase": operation,
                    "attempt": attempt,
                    "validation_error": exc.validation_error,
                    "invalid_output_preview": exc.invalid_output_preview,
                    "outcome": "retrying",
                }
                repairs.append(repair)
                correction = UserMessage(content=self._repair_instruction(exc))
                continue
            for item in repairs:
                item["outcome"] = "repaired"
            self._output_repairs.extend(repairs)
            return result

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
