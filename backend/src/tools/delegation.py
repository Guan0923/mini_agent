"""Tool specifications whose execution is supplied by the runtime subagent coordinator."""

from __future__ import annotations

from .base import Tool, ToolError
from .default_tools.schema import object_schema


def _runtime_only(**_arguments: object) -> str:
    raise ToolError("This tool can only run inside an AgentRunner with subagents enabled.")


def delegation_tools(max_tasks_per_batch: int = 8) -> tuple[Tool, Tool, Tool, Tool]:
    """Return persistent Agent-tree tools supplied by the runtime coordinator."""

    optional_source = {"source_thread_id": {"type": "string", "minLength": 1, "maxLength": 128}}
    return (
        Tool(
            "delegate_tasks",
            (
                "Creates persistent child Agent Threads in this Session and starts their initial tasks in the "
                "background. Results are delivered back automatically."
            ),
            _runtime_only,
            object_schema(
                {
                    **optional_source,
                    "subagent_count": {"type": "integer", "minimum": 1, "maximum": max_tasks_per_batch},
                    "subagent_name": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": max_tasks_per_batch,
                        "items": {"type": "string", "minLength": 1, "maxLength": 128},
                    },
                    "subagent_tasks": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": max_tasks_per_batch,
                        "items": {"type": "string", "minLength": 1, "maxLength": 20_000},
                    },
                    "context_transfer_strategy": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": max_tasks_per_batch,
                        "items": {
                            "type": "string",
                            "enum": ["share", "compaction_share", "independent"],
                        },
                    },
                },
                ["subagent_count", "subagent_name", "subagent_tasks", "context_transfer_strategy"],
            ),
            read_only=False,
        ),
        Tool(
            "send_agent_message",
            "Reliably sends one message to another opening node in the same Agent tree.",
            _runtime_only,
            object_schema(
                {
                    "source_thread_id": {"type": "string", "minLength": 1, "maxLength": 128},
                    "target_thread_id": {"type": "string", "minLength": 1, "maxLength": 128},
                    "subagent_tasks": {"type": "string", "minLength": 1, "maxLength": 20_000},
                },
                ["source_thread_id", "target_thread_id", "subagent_tasks"],
            ),
            read_only=False,
        ),
        Tool(
            "set_thread_node_status",
            "Opens or closes one direct child Agent Thread. Closing pauses it at the next safe boundary.",
            _runtime_only,
            object_schema(
                {
                    **optional_source,
                    "target_thread_id": {"type": "string", "minLength": 1, "maxLength": 128},
                    "thread_status": {"type": "string", "enum": ["opening", "closed"]},
                },
                ["target_thread_id", "thread_status"],
            ),
            read_only=False,
        ),
        Tool(
            "list_current_node_sub_thread",
            "Lists the direct child nodes of the current Agent Thread.",
            _runtime_only,
            object_schema(optional_source, []),
            read_only=True,
        ),
    )
