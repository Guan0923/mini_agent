"""Plan-only request_plan_review control protocol."""

from __future__ import annotations

from typing import Any

from jsonschema import Draft202012Validator

from backend.domain import ToolSpec

REQUEST_PLAN_REVIEW_NAME = "request_plan_review"

_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["plan"],
    "properties": {
        "plan": {"type": "string", "minLength": 1},
    },
}

REQUEST_PLAN_REVIEW_SPEC = ToolSpec(
    name=REQUEST_PLAN_REVIEW_NAME,
    description=(
        "Submit a complete implementation plan for explicit Plan Review. Use this only when a plan is useful, "
        "the important unknowns are resolved, and the user should choose whether to implement it. Call this "
        "control tool by itself. The plan must be non-empty Markdown. Prefer a plan title followed by Summary, "
        "Key Changes, Test Plan, and Assumptions sections, but adapt the content to the task."
    ),
    parameters=_PARAMETERS,
)

_VALIDATOR = Draft202012Validator(_PARAMETERS)


def parse_plan_review(arguments: dict[str, Any]) -> str:
    """Validate a model-generated Plan Review request and return its plan."""

    if not isinstance(arguments, dict):
        raise ValueError("request_plan_review arguments must be an object.")
    error = next(_VALIDATOR.iter_errors(arguments), None)
    if error is not None:
        raise ValueError(f"Invalid request_plan_review arguments at {error.json_path}: {error.message}")
    plan = arguments["plan"].strip()
    if not plan:
        raise ValueError("request_plan_review plan must not be blank.")
    return plan
