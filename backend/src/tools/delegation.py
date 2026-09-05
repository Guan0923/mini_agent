"""Tool specifications whose execution is supplied by the subagent coordinator."""

from __future__ import annotations

from .base import Tool, ToolError
from .default_tools.schema import object_schema


def _runtime_only(**_arguments: object) -> str:
    raise ToolError("This tool can only run inside an AgentRunner with subagents enabled.")


def delegation_tools() -> tuple[Tool, Tool, Tool, Tool, Tool]:
    """Return the current persistent Agent-tree tools."""

    optional_source = {
        "source_thread_id": {
            "type": "string",
            "minLength": 1,
            "maxLength": 128,
            "description": "The calling Agent Thread ID. Omit to use the actual calling Thread.",
        }
    }
    thread_path = {
        "type": "string",
        "pattern": r"^/root(?:/[^/]+)*$",
        "minLength": 5,
        "maxLength": 1000,
    }
    return (
        Tool(
            "delegate_tasks",
            "Creates one Agent at a new path and starts its assigned task.",
            _runtime_only,
            object_schema(
                {
                    **optional_source,
                    "subagent_path": {
                        **thread_path,
                        "description": "The complete path of the new Agent in the caller's root Agent tree.",
                    },
                    "subagent_task": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 20_000,
                        "description": "The initial task assigned to the new Agent.",
                    },
                    "context_transfer_strategy": {
                        "type": "string",
                        "enum": ["share", "compaction_share", "independent"],
                        "description": "How the actual caller's context is transferred to the new Agent.",
                    },
                },
                ["subagent_path", "subagent_task", "context_transfer_strategy"],
            ),
            read_only=False,
        ),
        Tool(
            "send_agent_message",
            "Assigns a task to another Agent in the same root tree and makes it run.",
            _runtime_only,
            object_schema(
                {
                    **optional_source,
                    "target_thread_path": {
                        **thread_path,
                        "description": "The complete path of the receiving Agent.",
                    },
                    "subagent_task": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 20_000,
                        "description": "The task delivered to the receiving Agent.",
                    },
                    "references": {
                        "type": "array",
                        "maxItems": 100,
                        "description": "Absolute file paths inside the target Session or project workspace.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "path": {
                                    "type": "string",
                                    "minLength": 1,
                                    "maxLength": 4000,
                                    "description": "A workspace: or project: file path. Absolute paths are accepted; bare paths use project when available, otherwise workspace.",
                                },
                            },
                            "required": ["path"],
                            "additionalProperties": False,
                        },
                    },
                    "need_reply": {
                        "type": "boolean",
                        "default": False,
                        "description": "Whether this task's Turn must report its terminal result to the actual sender.",
                    },
                },
                ["target_thread_path", "subagent_task"],
            ),
            read_only=False,
        ),
        Tool(
            "set_thread_node_status",
            "Changes a direct child Agent between running, paused, and success without sending a task.",
            _runtime_only,
            object_schema(
                {
                    **optional_source,
                    "target_thread_path": {
                        **thread_path,
                        "description": "The complete path of a direct child Agent.",
                    },
                    "thread_status": {
                        "type": "string",
                        "enum": ["running", "paused", "success"],
                        "description": "The requested status transition for the direct child Agent.",
                    },
                },
                ["target_thread_path", "thread_status"],
            ),
            read_only=False,
        ),
        Tool(
            "get_thread_node",
            "Reads one Agent node or all descendant Agent nodes and their latest task results.",
            _runtime_only,
            object_schema(
                {
                    **optional_source,
                    "target_thread_path": {
                        **thread_path,
                        "description": "Optional path of the source Agent or one of its descendants.",
                    },
                },
                [],
            ),
            read_only=True,
        ),
        Tool(
            "pause_current_turn",
            "Pauses the actual caller's current running Turn after this tool result is saved.",
            _runtime_only,
            object_schema({}, []),
            read_only=False,
        ),
    )


__all__ = ["delegation_tools"]
