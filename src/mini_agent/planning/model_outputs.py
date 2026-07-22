"""Validate and convert structured model output into domain values."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from mini_agent.domain import ExecutionPlan, ModelOutputError, PlanStep, ToolMessage, ToolSpec


def parse_json_object(raw: str, operation: str | None = None) -> dict[str, Any]:
    """Parse a JSON object, accepting one surrounding Markdown code fence."""

    normalized = raw.lstrip("\ufeff").strip()
    lines = normalized.splitlines()
    if len(lines) >= 2 and lines[0].strip().lower() in {"```", "```json"} and lines[-1].strip() == "```":
        normalized = "\n".join(lines[1:-1]).strip()
    try:
        payload = json.loads(normalized)
    except json.JSONDecodeError as exc:
        raise ModelOutputError("Model did not return valid JSON.", operation=operation, invalid_output=raw) from exc
    if not isinstance(payload, dict):
        raise ModelOutputError("Model JSON must be an object.", operation=operation, invalid_output=raw)
    return payload


def parse_execution_plan(
    raw: str,
    allowed_specs: list[ToolSpec],
    next_call_id: Callable[[], str],
) -> ExecutionPlan:
    """Validate a model-authored execution plan and allocate tool call ids."""

    payload = parse_json_object(raw)
    goal = payload.get("goal")
    steps = payload.get("steps")
    if not isinstance(goal, str) or not goal.strip() or not isinstance(steps, list):
        raise ModelOutputError(
            "Execution plan requires a goal and a steps array.", operation="plan", invalid_output=raw
        )

    allowed = {spec.name for spec in allowed_specs}
    parsed_steps: list[PlanStep] = []
    seen_ids: set[str] = set()
    for item in steps:
        if not isinstance(item, dict):
            raise ModelOutputError("Each plan step must be an object.", operation="plan", invalid_output=raw)
        step_id = item.get("id")
        description = item.get("description")
        success = item.get("success_criteria", "")
        name = item.get("tool")
        arguments = item.get("arguments")
        if not isinstance(step_id, str) or not step_id or step_id in seen_ids:
            raise ModelOutputError(
                "Plan step ids must be unique non-empty strings.", operation="plan", invalid_output=raw
            )
        if not isinstance(description, str) or not description.strip():
            raise ModelOutputError(
                "Plan step description must be non-empty text.", operation="plan", invalid_output=raw
            )
        if name not in allowed:
            raise ModelOutputError(f"Model requested unavailable tool: {name!r}.", operation="plan", invalid_output=raw)
        if not isinstance(arguments, dict):
            raise ModelOutputError("Plan step arguments must be an object.", operation="plan", invalid_output=raw)
        seen_ids.add(step_id)
        parsed_steps.append(
            PlanStep(
                id=step_id,
                description=description.strip(),
                success_criteria=success.strip() if isinstance(success, str) else "",
                tool_message=ToolMessage(name=name, call_id=next_call_id(), arguments=arguments),
            )
        )

    final_answer = payload.get("final_answer")
    if not parsed_steps and (not isinstance(final_answer, str) or not final_answer.strip()):
        raise ModelOutputError("A zero-step plan requires final_answer.", operation="plan", invalid_output=raw)
    if parsed_steps and final_answer is not None:
        raise ModelOutputError("A plan with steps must not contain final_answer.", operation="plan", invalid_output=raw)
    return ExecutionPlan(
        goal=goal.strip(),
        steps=parsed_steps,
        final_answer=final_answer.strip() if isinstance(final_answer, str) else None,
    )


def execution_plan_json(plan: ExecutionPlan) -> str:
    """Serialize the stable subset used as replanning context."""

    return json.dumps(
        {
            "goal": plan.goal,
            "steps": [
                {
                    "id": step.id,
                    "description": step.description,
                    "tool": step.tool_message.name,
                    "arguments": step.tool_message.arguments,
                    "status": step.status,
                }
                for step in plan.steps
            ],
        },
        ensure_ascii=False,
    )
