"""LLM planner formats behavior."""

from __future__ import annotations

import json
from typing import Any

from backend.domain import (
    ExecutionPlan,
    SystemMessage,
    ToolSpec,
)
from backend.runtime.core.context import AgentRuntime, PreparedResponse

from ..model_outputs import execution_plan_json, parse_execution_plan, parse_json_object


class FormatMixin:
    @staticmethod
    def _response_diagnostics(prepared: PreparedResponse) -> dict[str, str | int | None]:
        return {
            "finish_reason": prepared.finish_reason,
            "content_chars": len(prepared.message.content or ""),
            "reasoning_chars": len(prepared.message.reasoning or ""),
        }

    def _plan_system(self, *, dynamic: bool, allowed_specs: list[ToolSpec]) -> SystemMessage:
        stage = "first executable phase" if dynamic else "complete fixed plan"
        stage_guidance = (
            "Plan only the immediate next phase — do not try to predict the entire workflow."
            if dynamic
            else "Plan every step from start to finish. Each step must produce a verifiable intermediate result."
        )
        allowed_names = [spec.name for spec in allowed_specs]
        return SystemMessage(
            content=(
                f"Create the {stage}.\n\n"
                "Before writing the plan, consider:\n"
                "- What is the end goal? What does success look like?\n"
                "- What must be discovered or verified before acting?\n"
                "- Can each step's result be independently checked?\n"
                "- Are steps ordered correctly (dependencies first)?\n"
                "- Is this the minimal set of steps needed?\n\n"
                f"{stage_guidance}\n\n"
                "Return JSON only using "
                '{"goal":"text","steps":[{"id":"step_1","description":"text",'
                '"success_criteria":"text","tool":"tool name","arguments":{}}]}. '
                f"Allowed tool names are {json.dumps(allowed_names)}; every step must use one of them exactly. "
                "Never invent tools. For a response-only task, return exactly "
                '{"goal":"text","steps":[],"final_answer":"complete response"}.'
            )
        )

    @staticmethod
    def _json_object(raw: str, operation: str | None = None) -> dict[str, Any]:
        return parse_json_object(raw, operation)

    def _parse_execution_plan(
        self,
        raw: str,
        runtime: AgentRuntime,
        allowed_specs: list[ToolSpec],
    ) -> ExecutionPlan:
        return parse_execution_plan(raw, allowed_specs, runtime.next_tool_call_id)

    @staticmethod
    def _plan_json(plan: ExecutionPlan) -> str:
        return execution_plan_json(plan)
