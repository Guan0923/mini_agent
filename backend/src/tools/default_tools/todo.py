"""Turn-scoped, Redis-authoritative Todo update tool."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from backend.domain import TODO_STATUSES, TodoStateError

from ..base import Tool, ToolError, ToolInvocationContext
from .schema import object_schema


def _operation_schema() -> dict[str, object]:
    status = {
        "enum": list(TODO_STATUSES),
        "description": "Todo status: pending, in_progress, or completed.",
    }
    content = {
        "type": "string",
        "minLength": 1,
        "maxLength": 500,
        "description": "Non-blank Todo text, at most 500 characters.",
    }
    todo_id = {
        "type": "string",
        "pattern": r"^todo_[0-9a-f]{32}$",
        "description": "Backend-generated Todo ID from the latest successful snapshot.",
    }
    return {
        "oneOf": [
            {
                "type": "object",
                "required": ["op", "content", "status"],
                "properties": {
                    "op": {"const": "add", "description": "Append a new Todo."},
                    "content": content,
                    "status": status,
                },
                "additionalProperties": False,
            },
            {
                "type": "object",
                "required": ["op", "id"],
                "properties": {
                    "op": {"const": "update", "description": "Modify an existing Todo."},
                    "id": todo_id,
                    "content": content,
                    "status": status,
                },
                "anyOf": [{"required": ["content"]}, {"required": ["status"]}],
                "additionalProperties": False,
            },
            {
                "type": "object",
                "required": ["op", "id"],
                "properties": {
                    "op": {"const": "remove", "description": "Remove an existing Todo."},
                    "id": todo_id,
                },
                "additionalProperties": False,
            },
        ]
    }


def todo_tools() -> tuple[Tool, ...]:
    """Build the model-visible todo-list tool."""

    return (
        Tool(
            "update_todo_list",
            (
                "Atomically updates the current Turn's Todo list with add, update, and remove operations. "
                "Pass the revision from the latest successful result and use returned IDs for later updates. "
                "The backend returns the complete authoritative snapshot."
            ),
            _missing_context,
            object_schema(
                {
                    "expected_revision": {
                        "type": "integer",
                        "minimum": 0,
                        "description": "Revision from the latest successful authoritative snapshot.",
                    },
                    "operations": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 100,
                        "description": "One atomic batch of 1 to 100 add, update, or remove operations.",
                        "items": _operation_schema(),
                    },
                },
                ["expected_revision", "operations"],
            ),
            read_only=False,
            retryable=True,
            context_handler=_update_todo_list,
        ),
    )


def _missing_context(**_arguments: object) -> str:
    raise ToolError("update_todo_list requires an active Turn and Todo store.")


def _update_todo_list(
    context: ToolInvocationContext,
    expected_revision: int,
    operations: Sequence[Mapping[str, Any]],
) -> str:
    if not context.session_id or not context.turn_id or not context.call_id or context.todo_store is None:
        raise ToolError("update_todo_list requires an active Turn and Todo store.")
    try:
        result = context.todo_store.update(
            session_id=context.session_id,
            turn_id=context.turn_id,
            call_id=context.call_id,
            expected_revision=expected_revision,
            operations=operations,
        )
    except TodoStateError as exc:
        raise ToolError(json.dumps(exc.to_dict(), ensure_ascii=False, separators=(",", ":"))) from exc
    return json.dumps(result.to_dict(), ensure_ascii=False, separators=(",", ":"))
