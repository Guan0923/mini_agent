"""Tool specifications whose execution is supplied by the runtime subagent coordinator."""

from __future__ import annotations

from .base import Tool, ToolError
from .default_tools.schema import object_schema


def _runtime_only(**_arguments: object) -> str:
    raise ToolError("This tool can only run inside an AgentRunner with subagents enabled.")


def delegation_tools(max_tasks_per_batch: int = 8) -> tuple[Tool, Tool, Tool, Tool]:
    """Return persistent Agent-tree tools supplied by the runtime coordinator."""

    optional_parent_source = {
        "source_thread_id": {
            "type": "string",
            "minLength": 1,
            "maxLength": 128,
            "description": (
                "The parent Agent Thread ID. When omitted, it is automatically set to the current calling Agent "
                "Thread ID."
            ),
        }
    }
    optional_sending_source = {
        "source_thread_id": {
            "type": "string",
            "minLength": 1,
            "maxLength": 128,
            "description": (
                "The sending Agent Thread ID. When omitted, it is automatically set to the current calling Agent "
                "Thread ID."
            ),
        }
    }
    optional_references = {
        "references": {
            "type": "array",
            "maxItems": 100,
            "description": "Structured project or upload file references delivered with the message.",
            "items": {
                "type": "object",
                "properties": {
                    "source": {
                        "type": "string",
                        "enum": ["project", "upload"],
                        "description": "Whether the referenced path belongs to the project or Session uploads.",
                    },
                    "path": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 1000,
                        "description": "Workspace-relative project path or Session upload path.",
                    },
                },
                "required": ["source", "path"],
                "additionalProperties": False,
            },
        }
    }
    return (
        Tool(
            "delegate_tasks",
            (
                "Creates persistent child Agent Threads under the current Agent Thread and starts their assigned "
                "tasks in the background."
            ),
            _runtime_only,
            object_schema(
                {
                    **optional_parent_source,
                    "subagent_count": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": max_tasks_per_batch,
                        "description": (
                            "The number of child Agent Threads to create and the required length of subagent_name, "
                            "subagent_tasks, and context_transfer_strategy."
                        ),
                    },
                    "subagent_name": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": max_tasks_per_batch,
                        "description": (
                            "The unique name of each child Agent Thread, in the same order as subagent_tasks and "
                            "context_transfer_strategy."
                        ),
                        "items": {"type": "string", "minLength": 1, "maxLength": 128},
                    },
                    "subagent_tasks": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": max_tasks_per_batch,
                        "description": (
                            "The initial task assigned to each child Agent Thread, in the same order as "
                            "subagent_name and context_transfer_strategy."
                        ),
                        "items": {"type": "string", "minLength": 1, "maxLength": 20_000},
                    },
                    "context_transfer_strategy": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": max_tasks_per_batch,
                        "description": (
                            "The context transfer mode for each child Agent Thread, in matching order: share copies "
                            "the current context, compaction_share provides a compacted summary, and independent "
                            "provides no parent context."
                        ),
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
            "Queues a task message for another Agent Thread and starts the target thread if it is idle.",
            _runtime_only,
            object_schema(
                {
                    **optional_sending_source,
                    **optional_references,
                    "target_thread_id": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 128,
                        "description": "The ID of the Agent Thread receiving the message.",
                    },
                    "subagent_tasks": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 20_000,
                        "description": "The task message to deliver to the target Agent Thread.",
                    },
                },
                ["target_thread_id", "subagent_tasks"],
            ),
            read_only=False,
        ),
        Tool(
            "set_thread_node_status",
            (
                "Changes a direct child Agent Thread between opening and closed. Opening wakes queued work; "
                "closing pauses execution at the next safe boundary."
            ),
            _runtime_only,
            object_schema(
                {
                    **optional_parent_source,
                    "target_thread_id": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 128,
                        "description": "The ID of the direct child Agent Thread whose status will be changed.",
                    },
                    "thread_status": {
                        "type": "string",
                        "enum": ["opening", "closed"],
                        "description": "The status to set: opening or closed.",
                    },
                },
                ["target_thread_id", "thread_status"],
            ),
            read_only=False,
        ),
        Tool(
            "list_current_node_sub_thread",
            (
                "Lists the direct child Agent Threads of the current Agent Thread, returning each child's ID, "
                "path, assigned task, and status."
            ),
            _runtime_only,
            object_schema(optional_parent_source, []),
            read_only=True,
        ),
    )
