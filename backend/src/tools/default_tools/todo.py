"""Session todo-list tool: the agent records and adjusts its working task list."""

from __future__ import annotations

from collections import Counter
from typing import Any

from ..base import Tool, ToolError
from .schema import object_schema

TODO_STATUSES = ("pending", "in_progress", "completed")


def todo_tools() -> tuple[Tool, ...]:
    """Build the model-visible todo-list tool."""

    return (
        Tool(
            "todo_write",
            (
                "Creates or replaces the current task list, tracks each task as pending, in progress, or "
                "completed, and returns the number of tasks in each status."
            ),
            _todo_write,
            object_schema(
                {
                    "todos": {
                        "type": "array",
                        "minItems": 0,
                        "maxItems": 100,
                        "description": (
                            "The complete task list that replaces the existing list. Use an empty array to clear "
                            "the list."
                        ),
                        "items": {
                            "type": "object",
                            "required": ["content", "status"],
                            "properties": {
                                "content": {
                                    "type": "string",
                                    "minLength": 1,
                                    "maxLength": 500,
                                    "description": (
                                        "The task text. It uniquely identifies the task and must be unique within "
                                        "the list."
                                    ),
                                },
                                "status": {
                                    "enum": list(TODO_STATUSES),
                                    "description": ("The task's current status: pending, in_progress, or completed."),
                                },
                            },
                        },
                    }
                },
                ["todos"],
            ),
            read_only=False,
            retryable=True,
        ),
    )


def _todo_write(todos: list[Any]) -> str:
    """Validate one full replacement of the session task list and echo its counts.

    The tool is deliberately stateless: the list itself lives in the
    conversation tree as the tool-call arguments, so validation and the
    counting echo are the only responsibilities here.
    """

    contents: list[str] = []
    statuses: list[str] = []
    for index, raw in enumerate(todos):
        if not isinstance(raw, dict):
            raise ToolError(f"Invalid todo item at index {index}: expected an object.")
        content = raw.get("content")
        status = raw.get("status")
        if not isinstance(content, str) or not content.strip():
            raise ToolError(f"Invalid todo item at index {index}: content must be a non-blank string.")
        if status not in TODO_STATUSES:
            raise ToolError(f"Invalid todo item at index {index}: status must be one of {', '.join(TODO_STATUSES)}.")
        if content in contents:
            raise ToolError(f"Duplicate todo content: {content!r}; each item's content must be unique.")
        contents.append(content)
        statuses.append(status)
    counts = Counter(statuses)
    return (
        f"Todo list updated: {len(contents)} items — "
        f"pending: {counts['pending']}, in_progress: {counts['in_progress']}, completed: {counts['completed']}"
    )
