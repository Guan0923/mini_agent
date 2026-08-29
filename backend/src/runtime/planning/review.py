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
        "plan": {
            "type": "string",
            "minLength": 1,
            "description": "The complete implementation plan in Markdown.",
        },
    },
}

REQUEST_PLAN_REVIEW_SPEC = ToolSpec(
    name=REQUEST_PLAN_REVIEW_NAME,
    description=(
        "Pauses the current Plan-mode run to present a complete implementation plan for user review and returns "
        "the user's decision."
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
