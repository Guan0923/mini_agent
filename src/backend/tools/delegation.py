"""Tool specifications whose execution is supplied by the runtime subagent coordinator."""

from __future__ import annotations

from .base import Tool, ToolError
from .default_tools.schema import object_schema


def _runtime_only(**_arguments: object) -> str:
    raise ToolError("This tool can only run inside an AgentRunner with subagents enabled.")


def delegation_tools() -> tuple[Tool, Tool]:
    """Return the model-visible tools used to delegate and inspect child work."""

    task = object_schema(
        {
            "id": {"type": "string", "minLength": 1, "maxLength": 128},
            "task": {"type": "string", "minLength": 1, "maxLength": 20_000},
        },
        ["id", "task"],
    )
    return (
        Tool(
            "delegate_tasks",
            (
                "Delegates independent, self-contained tasks to subagents. Each subagent has the full "
                "workspace tool set. Use distinct ids and only group work that can proceed independently."
            ),
            _runtime_only,
            object_schema({"tasks": {"type": "array", "minItems": 1, "items": task}}, ["tasks"]),
            read_only=False,
        ),
        Tool(
            "get_subagent_results",
            "Reads a page of results from a completed subagent batch.",
            _runtime_only,
            object_schema(
                {
                    "batch_id": {"type": "string", "minLength": 1},
                    "cursor": {"type": "integer", "minimum": 0, "default": 0},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
                },
                ["batch_id"],
            ),
        ),
    )
