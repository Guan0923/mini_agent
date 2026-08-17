"""Project canonical runtime nodes into the Web conversation transcript."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from backend.domain import terminal_error_payload, terminal_error_text

_HIDDEN_RECOVERABLE_EVENTS = frozenset({"tool_failed", "tool_recovery", "model_repair", "model_retry"})


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    return json.dumps(value, ensure_ascii=False, indent=2)


def _blocks(message: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    content = message.get("content")
    return [item for item in content if isinstance(item, Mapping)] if isinstance(content, list) else []


def _timeline_text(blocks: list[Mapping[str, Any]]) -> str:
    """Fold canonical content blocks into the timeline preview text."""

    text_parts: list[str] = []
    has_other = False
    for block in blocks:
        if block.get("type") == "text" and isinstance(block.get("text"), str):
            value = str(block["text"]).strip()
            if value:
                text_parts.append(value)
        else:
            has_other = True
    text = " ".join(text_parts).strip()
    if not text and has_other:
        return "[非文本内容]"
    if text and has_other:
        return f"{text} …"
    return text


def _timeline_time(node: Any) -> int:
    """Convert a canonical UTC timestamp to epoch milliseconds."""

    try:
        value = datetime.fromisoformat(str(getattr(node, "timestamp", "")))
        return max(0, int(value.timestamp() * 1000))
    except (TypeError, ValueError, OverflowError, OSError):
        return 0


def _terminal_error(node: Any, message: Mapping[str, Any] | None = None) -> str | None:
    """Return the durable reason for a terminal node.

    A node can be observed between the placeholder create and its final
    delete, so the message payload is not guaranteed to be present.  The
    projection must still expose a useful reason instead of dropping that
    node from the transcript.
    """

    status = getattr(node, "status", "")
    if status not in {"failed", "abort"}:
        return None
    if message is not None:
        error = message.get("error")
        if isinstance(error, Mapping):
            return terminal_error_text(error)
        if isinstance(error, str) and error:
            return error
    fallback_status = "failed" if status == "failed" else "abort"
    return terminal_error_text(terminal_error_payload(fallback_status))


def _terminal_entry(node: Any, error: str) -> dict[str, Any]:
    return {
        "id": f"{node.session_id}:{node.id}:assistant",
        "run_id": None,
        "role": "assistant",
        "content": error,
        "events": [],
        "error": error,
        "status": node.status,
        "source_node_id": node.id,
    }


def _ordered_nodes(nodes: list[Any]) -> list[Any]:
    """Order a partial tree by parent edges before using timestamp ties.

    ``utc_iso`` timestamps can share the same clock tick on fast local runs.
    Sorting those nodes by UUID would occasionally place an assistant before
    its user message, making the final transcript appear to lose an error.
    A small stable tree walk keeps every parent before its children while
    preserving deterministic timestamp order between siblings.
    """

    values = list(nodes)
    by_key = {(getattr(node, "session_id", ""), getattr(node, "id", "")): node for node in values}
    children: dict[tuple[str, str], list[Any]] = {}
    roots: list[Any] = []
    for node in values:
        parent_id = getattr(node, "parent_id", "")
        parent_key = (getattr(node, "parent_session_id", ""), parent_id)
        if parent_id and parent_key in by_key:
            children.setdefault(parent_key, []).append(node)
        else:
            roots.append(node)

    def sort_key(node: Any) -> tuple[str, str]:
        return (str(getattr(node, "timestamp", "")), str(getattr(node, "id", "")))

    ordered: list[Any] = []
    seen: set[tuple[str, str]] = set()

    def visit(node: Any) -> None:
        key = (getattr(node, "session_id", ""), getattr(node, "id", ""))
        if key in seen:
            return
        seen.add(key)
        ordered.append(node)
        for child in sorted(children.get(key, []), key=sort_key):
            visit(child)

    for root in sorted(roots, key=sort_key):
        visit(root)
    # Be defensive when malformed/partial input contains a cycle.
    for node in sorted(values, key=sort_key):
        visit(node)
    return ordered


def project_node_transcript(nodes: list[Any]) -> list[dict[str, Any]]:
    """Return one user/assistant pair per turn while preserving run details."""

    result: list[dict[str, Any]] = []
    current_assistant: dict[str, Any] | None = None
    timeline_seq = 0
    for node in _ordered_nodes(nodes):
        data = getattr(node, "data", {})
        if not isinstance(data, Mapping):
            data = {}
        if not data:
            error = _terminal_error(node)
            if error is not None:
                result.append(_terminal_entry(node, error))
                current_assistant = None
            continue
        if data.get("type") != "message":
            error = _terminal_error(node)
            if error is not None:
                result.append(_terminal_entry(node, error))
                current_assistant = None
            continue
        message = data.get("message")
        if not isinstance(message, Mapping):
            error = _terminal_error(node)
            if error is not None:
                result.append(_terminal_entry(node, error))
                current_assistant = None
            continue
        role = str(message.get("role") or "")
        blocks = _blocks(message)
        if role == "user":
            timeline_seq += 1
            source = str(message.get("source") or "user")
            if source not in {"user", "steering"}:
                source = "user"
            content = "".join(_text(block.get("text")) for block in blocks if block.get("type") in {"text", "bash"})
            payload: dict[str, Any] = {
                "id": f"{node.session_id}:{node.id}",
                "run_id": None,
                "role": "user",
                "content": content,
                "events": [],
                "source_node_id": node.parent_id or None,
                "timeline_seq": timeline_seq,
                "timeline_time": _timeline_time(node),
                "timeline_text": _timeline_text(blocks),
                "timeline_source": source,
            }
            references = message.get("references")
            if isinstance(references, list) and all(isinstance(item, dict) for item in references):
                payload["references"] = [
                    {
                        "source": str(item.get("source") or ""),
                        "path": str(item.get("path") or ""),
                    }
                    for item in references
                    if isinstance(item.get("source"), str)
                    and isinstance(item.get("path"), str)
                    and item["source"] in {"project", "upload"}
                    and item["path"]
                ]
            result.append(payload)
            current_assistant = None
            continue
        if role not in {"assistant", "tool_result"}:
            error = _terminal_error(node, message)
            if error is not None:
                result.append(_terminal_entry(node, error))
                current_assistant = None
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
        error_text = _terminal_error(node, message)
        if error_text is not None:
            current_assistant["error"] = error_text
            current_assistant["status"] = node.status

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
                if failed:
                    continue
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
