"""Plan-only request_user_input control protocol."""

from __future__ import annotations

import json
from typing import Any

from jsonschema import Draft202012Validator

from backend.domain import ToolSpec

from ..core.contracts import QuestionOption, UserQuestion

REQUEST_USER_INPUT_NAME = "request_user_input"
OTHER_OPTION_LABEL = "其他"

_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["questions"],
    "properties": {
        "questions": {
            "type": "array",
            "minItems": 1,
            "description": "The questions to present to the user.",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id", "header", "question", "options"],
                "properties": {
                    "id": {
                        "type": "string",
                        "pattern": "^[a-z][a-z0-9_]{0,63}$",
                        "description": (
                            "The unique identifier used to associate the returned answer with this question."
                        ),
                    },
                    "header": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 12,
                        "description": "The short header displayed for the question.",
                    },
                    "question": {
                        "type": "string",
                        "minLength": 1,
                        "description": "The question text displayed to the user.",
                    },
                    "options": {
                        "type": "array",
                        "description": (
                            "The mutually exclusive choices for the question. The client adds a free-form Other choice."
                        ),
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["label", "description"],
                            "properties": {
                                "label": {
                                    "type": "string",
                                    "minLength": 1,
                                    "description": "The short label displayed for the choice.",
                                },
                                "description": {
                                    "type": "string",
                                    "minLength": 1,
                                    "description": (
                                        "The explanation of the choice's impact or trade-off displayed to the user."
                                    ),
                                },
                            },
                        },
                    },
                },
            },
        }
    },
}

REQUEST_USER_INPUT_SPEC = ToolSpec(
    name=REQUEST_USER_INPUT_NAME,
    description=(
        "Pauses the current Plan-mode run to present multiple-choice questions to the user and returns the selected "
        "or free-form answers."
    ),
    parameters=_PARAMETERS,
)

_VALIDATOR = Draft202012Validator(_PARAMETERS)


def _required_text(value: str, field: str) -> str:
    text = value.strip()
    if not text:
        raise ValueError(f"request_user_input {field} must not be blank.")
    return text


def parse_user_input_questions(arguments: dict[str, Any]) -> tuple[UserQuestion, ...]:
    """Validate model arguments and convert them to immutable runtime values."""

    if not isinstance(arguments, dict):
        raise ValueError("request_user_input arguments must be an object.")
    error = next(_VALIDATOR.iter_errors(arguments), None)
    if error is not None:
        raise ValueError(f"Invalid request_user_input arguments at {error.json_path}: {error.message}")

    parsed: list[UserQuestion] = []
    seen_ids: set[str] = set()
    for raw_question in arguments["questions"]:
        question_id = raw_question["id"]
        if question_id in seen_ids:
            raise ValueError("request_user_input question ids must be unique.")
        seen_ids.add(question_id)

        options: list[QuestionOption] = []
        seen_labels: set[str] = set()
        for raw_option in raw_question["options"]:
            label = _required_text(raw_option["label"], "option label")
            normalized = label.casefold()
            if normalized == OTHER_OPTION_LABEL.casefold():
                continue
            if normalized in seen_labels:
                raise ValueError(f"Question {question_id!r} option labels must be unique.")
            seen_labels.add(normalized)
            options.append(
                QuestionOption(
                    label,
                    _required_text(raw_option["description"], "option description"),
                )
            )

        parsed.append(
            UserQuestion(
                id=question_id,
                header=_required_text(raw_question["header"], "question header"),
                question=_required_text(raw_question["question"], "question text"),
                options=tuple(options),
            )
        )
    return tuple(parsed)


def validate_user_input_answers(
    questions: tuple[UserQuestion, ...],
    answers: dict[str, list[str]] | None,
) -> dict[str, list[str]]:
    """Require one non-empty answer or an explicit skip for every question."""

    if not isinstance(answers, dict):
        raise ValueError("Question answers must be an object.")
    expected = {question.id for question in questions}
    if set(answers) != expected:
        raise ValueError("Question answers must contain exactly the requested question ids.")
    normalized: dict[str, list[str]] = {}
    for question in questions:
        values = answers[question.id]
        if not isinstance(values, list) or len(values) > 1:
            raise ValueError(f"Question {question.id!r} requires one answer or an empty list.")
        if not values:
            normalized[question.id] = []
            continue
        if not isinstance(values[0], str) or not values[0].strip():
            raise ValueError(f"Question {question.id!r} requires exactly one non-empty answer.")
        normalized[question.id] = [values[0].strip()]
    return normalized


def format_user_input_answers(answers: dict[str, list[str]]) -> str:
    """Serialize answers using the Codex request_user_input tool-result shape."""

    payload = {"answers": {question_id: {"answers": values} for question_id, values in answers.items()}}
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
