"""One model exchange plus message-tree projection helpers."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from backend.domain import (
    CHECKPOINT_PREAMBLE,
    AssistantMessage,
    ChatMessage,
    ToolMessage,
    ToolSpec,
    UserMessage,
)
from backend.domain.file_paths import is_reference_path
from backend.domain.runtime_state import RuntimeState as RuntimeTreeNode

RuntimeOperation = Literal[
    "skill_selection",
    "decision",
    "plan",
    "summarize",
    "title",
    "finalize",
]
OutputMode = Literal["text", "json", "tools"]
RuntimeStatus = Literal["idle", "running"]


@dataclass
class PreparedResponse:
    message: AssistantMessage
    usage: dict[str, Any] | None = None
    response_id: str | None = None
    model: str | None = None
    finish_reason: str | None = None
    provider_metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class RuntimeExchange:
    """Transient state for exactly one model request and response."""

    operation: RuntimeOperation | None = None
    exchange_id: str | None = None
    output_mode: OutputMode = "text"
    allowed_tools: list[ToolSpec] = field(default_factory=list)
    operation_tools: list[ToolSpec] = field(default_factory=list)
    messages: list[ChatMessage] = field(default_factory=list)
    stream: bool = False
    request: dict[str, Any] | None = None
    raw_response: dict[str, Any] | Iterable[dict[str, Any]] | None = None
    wire_request: dict[str, Any] | None = None
    wire_response: Any = None
    transport_metadata: dict[str, Any] = field(default_factory=dict)
    prepared_response: PreparedResponse | None = None
    context: dict[str, Any] = field(default_factory=dict)
    on_reasoning: Callable[[str], None] | None = None
    on_content: Callable[[str], None] | None = None
    required_tool_name: str | None = None

    def reset(self) -> None:
        self.operation = None
        self.exchange_id = None
        self.output_mode = "text"
        self.allowed_tools = []
        self.operation_tools = []
        self.messages = []
        self.stream = False
        self.request = None
        self.raw_response = None
        self.wire_request = None
        self.wire_response = None
        self.transport_metadata = {}
        self.prepared_response = None
        self.context = {}
        self.on_reasoning = None
        self.on_content = None
        self.required_tool_name = None


def new_tool_call_id() -> str:
    return f"call_{uuid4().hex}"


def new_exchange_id() -> str:
    return f"exchange_{uuid4().hex}"


def successful_items(items: Sequence[object]) -> list[Mapping[str, Any]]:
    """Return only complete canonical Items for provider context projection."""

    return [item for item in items if isinstance(item, Mapping) and item.get("status") == "success"]


def _assistant_items(items: Sequence[object]) -> list[Mapping[str, Any]]:
    """Keep complete ordinary Items plus valid completed tool call/result pairs."""

    mapped = [item for item in items if isinstance(item, Mapping)]
    tool_items: dict[str, list[tuple[int, Mapping[str, Any]]]] = {}
    for index, item in enumerate(mapped):
        if item.get("type") not in {"tool_call", "tool_result"}:
            continue
        call_id = item.get("call_id")
        if isinstance(call_id, str) and call_id:
            tool_items.setdefault(call_id, []).append((index, item))

    valid_tool_indices: set[int] = set()
    for entries in tool_items.values():
        if len(entries) != 2:
            continue
        (call_index, call), (result_index, tool_result) = entries
        if call.get("type") != "tool_call" or tool_result.get("type") != "tool_result":
            continue
        status = call.get("status")
        if status not in {"success", "failed"} or tool_result.get("status") != status:
            continue
        if "tool" in tool_result and tool_result.get("tool") != call.get("name"):
            continue
        valid_tool_indices.update({call_index, result_index})

    return [
        item
        for index, item in enumerate(mapped)
        if index in valid_tool_indices
        or (item.get("type") not in {"tool_call", "tool_result"} and item.get("status") == "success")
    ]


def _chat_messages_from_nodes(nodes: Sequence[RuntimeTreeNode]) -> list[ChatMessage]:
    """Project each selected Turn version into the existing planner port."""

    result: list[ChatMessage] = []
    for node in nodes:
        for message in node.selected_messages:
            if message.get("role") == "user":
                blocks = successful_items(message.get("content", []))
                user_text = "".join(
                    str(item.get("text") or item.get("summary") or "")
                    for item in blocks
                    if item.get("type") in {"text", "bash", "compaction"}
                )
                references: list[str] = []
                for item in blocks:
                    for reference in item.get("references", []) if isinstance(item.get("references"), list) else []:
                        if isinstance(reference, Mapping) and reference.get("path"):
                            raw_path = str(reference["path"])
                            if is_reference_path(raw_path):
                                source = str(reference.get("source") or "")
                                suffix = f" ({source})" if source else ""
                                references.append(f"- @{Path(raw_path).as_posix()}{suffix}")
                if references:
                    user_text = f"{user_text}\n\nFile references:\n" + "\n".join(references)
                if user_text:
                    result.append(UserMessage(content=user_text))
                continue

            blocks = _assistant_items(message.get("content", []))
            summary = next((str(item.get("summary") or "") for item in blocks if item.get("type") == "compaction"), "")
            if summary:
                result.append(UserMessage(content=f"{CHECKPOINT_PREAMBLE}\n\n{summary}"))
            text_parts = [str(item.get("text") or "") for item in blocks if item.get("type") in {"text", "bash"}]
            reasoning_parts = [str(item.get("text") or "") for item in blocks if item.get("type") == "reasoning"]
            calls: dict[str, ToolMessage] = {}
            completed_call_ids = {
                str(item.get("call_id") or "")
                for item in blocks
                if item.get("type") == "tool_result" and item.get("call_id")
            }
            for item in blocks:
                kind = item.get("type")
                call_id = str(item.get("call_id") or "")
                if kind == "tool_call" and call_id in completed_call_ids:
                    calls[call_id] = ToolMessage(
                        name=str(item.get("name") or "unknown"),
                        call_id=call_id,
                        arguments=(
                            dict(item.get("arguments") or {}) if isinstance(item.get("arguments"), Mapping) else {}
                        ),
                    )
                elif kind == "tool_result" and call_id:
                    tool = calls.get(call_id)
                    if tool is None:
                        continue
                    content = item.get("content")
                    tool.content = (
                        content if isinstance(content, str) else json.dumps(content, ensure_ascii=False, default=str)
                    )
                    tool.status = "succeeded" if item.get("status") == "success" else "failed"
                    tool.retryable = item.get("retryable") if isinstance(item.get("retryable"), bool) else None
                    tool.failure_code = item.get("failure_code") if isinstance(item.get("failure_code"), str) else None
                elif kind == "error":
                    text_parts.append(str(item.get("message") or "Execution failed."))
                elif kind == "subagent" and item.get("event") == "agent_report":
                    report = str(item.get("text") or "")
                    if report:
                        result.append(AssistantMessage(name="subagent_report", content=report))
            assistant = AssistantMessage(
                content="".join(text_parts) or None,
                reasoning="".join(reasoning_parts) or None,
                tool_messages=list(calls.values()),
            )
            if assistant.content or assistant.reasoning or assistant.tool_messages:
                result.append(assistant)
    return result
