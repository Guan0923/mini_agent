"""Project canonical runtime nodes into the Web conversation transcript."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    return json.dumps(value, ensure_ascii=False, indent=2)


def _blocks(message: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    content = message.get("content")
    return [item for item in content if isinstance(item, Mapping)] if isinstance(content, list) else []


def project_node_transcript(nodes: list[Any]) -> list[dict[str, Any]]:
    """Return one user/assistant pair per turn while preserving run details."""

    result: list[dict[str, Any]] = []
    current_assistant: dict[str, Any] | None = None
    for node in nodes:
        data = getattr(node, "data", {})
        if not isinstance(data, Mapping) or data.get("type") != "message":
            continue
        message = data.get("message")
        if not isinstance(message, Mapping):
            continue
        role = str(message.get("role") or "")
        blocks = _blocks(message)
        if role == "user":
            content = "".join(_text(block.get("text")) for block in blocks if block.get("type") in {"text", "bash"})
            result.append(
                {
                    "id": f"{node.session_id}:{node.id}",
                    "run_id": None,
                    "role": "user",
                    "content": content,
                    "events": [],
                    "source_node_id": node.parent_id or None,
                }
            )
            current_assistant = None
            continue
        if role not in {"assistant", "tool_result"} or not result:
            continue
        if current_assistant is None:
            current_assistant = {
                "id": f"{node.session_id}:{node.id}:assistant",
                "run_id": None,
                "role": "assistant",
                "content": "",
                "events": [],
                "source_node_id": node.id,
            }
            result.append(current_assistant)
        current_assistant["source_node_id"] = node.id
        if isinstance(message.get("run_id"), str) and message["run_id"]:
            current_assistant["run_id"] = message["run_id"]

        reasoning = "".join(_text(block.get("text")) for block in blocks if block.get("type") == "reasoning")
        if reasoning:
            current_assistant["events"].append(
                {
                    "kind": "thinking",
                    "message": reasoning,
                    "data": {"node_id": node.id, "completed": node.status != "abort"},
                }
            )
        for block in blocks:
            kind = block.get("type")
            if kind in {"text", "bash"}:
                current_assistant["content"] += _text(block.get("text"))
            elif kind == "tool_call":
                tool = str(block.get("name") or block.get("tool") or "工具")
                current_assistant["events"].append(
                    {
                        "kind": "tool_call",
                        "message": tool,
                        "data": {**dict(block), "tool": tool},
                    }
                )
            elif kind == "tool_result":
                failed = block.get("status") == "failed" or node.status == "failed"
                current_assistant["events"].append(
                    {
                        "kind": "tool_failed" if failed else "tool_result",
                        "message": _text(block.get("content")),
                        "data": {
                            **dict(block),
                            "tool": str(block.get("tool") or "工具"),
                            "result": block.get("content"),
                        },
                    }
                )
    return result


__all__ = ["project_node_transcript"]
