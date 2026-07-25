"""Conversion from provider-neutral chat messages to DeepSeek wire messages."""

from __future__ import annotations

import json
from typing import Any

from backend.domain import AssistantMessage, ChatMessage, SystemMessage, ToolSpec, UserMessage
from backend.runtime.core.context import AgentRuntime

from ..errors import ModelRequestError
from .common import _TOOL_NAME, _merge_extra_fields, _provider_options


def _optional_name(message: SystemMessage | UserMessage | AssistantMessage) -> dict[str, str]:
    if not isinstance(message.name, str) or not message.name:
        raise ModelRequestError("DeepSeek message name must be a non-empty string.")
    return {"name": message.name} if message.name != message.role else {}


def _tool_definition(spec: ToolSpec) -> dict[str, Any]:
    if not _TOOL_NAME.fullmatch(spec.name):
        raise ModelRequestError(
            f"DeepSeek tool name {spec.name!r} must contain 1-64 letters, digits, underscores, or hyphens."
        )
    if not isinstance(spec.description, str):
        raise ModelRequestError(f"DeepSeek tool {spec.name!r} description must be text.")
    if not isinstance(spec.parameters, dict):
        raise ModelRequestError(f"DeepSeek tool {spec.name!r} parameters must be a JSON Schema object.")
    function: dict[str, Any] = {"name": spec.name, "description": spec.description}
    if spec.parameters:
        function["parameters"] = spec.parameters
    options = _provider_options(spec)
    unknown = set(options) - {"strict", "extra_body"}
    if unknown:
        raise ModelRequestError(f"Unknown DeepSeek tool option(s): {', '.join(sorted(unknown))}.")
    if "strict" in options:
        if not isinstance(options["strict"], bool):
            raise ModelRequestError(f"DeepSeek tool {spec.name!r} strict must be boolean.")
        function["strict"] = options["strict"]
    _merge_extra_fields(
        function,
        options.get("extra_body"),
        protected={"name", "description", "parameters", "strict"},
        label=f"tool {spec.name!r}",
    )
    return {"type": "function", "function": function}


def _wire_messages_from(source: list[ChatMessage]) -> list[dict[str, Any]]:
    if not source:
        raise ModelRequestError("DeepSeek messages must contain at least one message.")
    wire: list[dict[str, Any]] = []
    seen_call_ids: set[str] = set()
    for position, message in enumerate(source):
        if isinstance(message, SystemMessage):
            options = _provider_options(message)
            unknown = set(options) - {"extra_body"}
            if unknown:
                raise ModelRequestError(f"Unknown DeepSeek system message option(s): {', '.join(sorted(unknown))}.")
            item: dict[str, Any] = {"role": "system", "content": message.content or ""}
            item.update(_optional_name(message))
            _merge_extra_fields(
                item,
                options.get("extra_body"),
                protected={"role", "content", "name"},
                label="system message",
            )
            wire.append(item)
            continue
        if isinstance(message, UserMessage):
            options = _provider_options(message)
            unknown = set(options) - {"extra_body"}
            if unknown:
                raise ModelRequestError(f"Unknown DeepSeek user message option(s): {', '.join(sorted(unknown))}.")
            item = {"role": "user", "content": message.content or ""}
            item.update(_optional_name(message))
            _merge_extra_fields(
                item,
                options.get("extra_body"),
                protected={"role", "content", "name"},
                label="user message",
            )
            wire.append(item)
            continue
        if not isinstance(message, AssistantMessage):
            raise ModelRequestError(f"Unsupported internal message type: {type(message).__name__}.")
        options = _provider_options(message)
        unknown = set(options) - {"prefix", "extra_body", "response"}
        if unknown:
            raise ModelRequestError(f"Unknown DeepSeek assistant message option(s): {', '.join(sorted(unknown))}.")
        prefix = options.get("prefix")
        if prefix is not None and not isinstance(prefix, bool):
            raise ModelRequestError("DeepSeek assistant prefix must be boolean.")
        if prefix and position != len(source) - 1:
            raise ModelRequestError("DeepSeek assistant prefix is only valid on the last input message.")
        if not message.tool_messages:
            assistant: dict[str, Any] = {"role": "assistant", "content": message.content}
            assistant.update(_optional_name(message))
            if "prefix" in options:
                assistant["prefix"] = prefix
            if prefix and message.reasoning is not None:
                assistant["reasoning_content"] = message.reasoning
            _merge_extra_fields(
                assistant,
                options.get("extra_body"),
                protected={"role", "content", "name", "prefix", "reasoning_content", "tool_calls"},
                label="assistant message",
            )
            wire.append(assistant)
            continue

        tool_calls: list[dict[str, Any]] = []
        for tool in message.tool_messages:
            if tool.call_id in seen_call_ids:
                raise ModelRequestError(f"Duplicate tool call id in message history: {tool.call_id}.")
            seen_call_ids.add(tool.call_id)
            if tool.status == "pending" or tool.content is None:
                raise ModelRequestError(f"Tool call {tool.call_id} has no result and cannot be sent to DeepSeek.")
            tool_calls.append(
                {
                    "id": tool.call_id,
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "arguments": json.dumps(tool.arguments, ensure_ascii=False, separators=(",", ":")),
                    },
                }
            )
        assistant: dict[str, Any] = {
            "role": "assistant",
            "content": message.content,
            "tool_calls": tool_calls,
        }
        assistant.update(_optional_name(message))
        if message.reasoning is not None:
            assistant["reasoning_content"] = message.reasoning
        if "prefix" in options:
            assistant["prefix"] = prefix
        _merge_extra_fields(
            assistant,
            options.get("extra_body"),
            protected={"role", "content", "name", "prefix", "reasoning_content", "tool_calls"},
            label="assistant message",
        )
        wire.append(assistant)
        wire.extend(
            {"role": "tool", "tool_call_id": tool.call_id, "content": tool.content} for tool in message.tool_messages
        )
    return wire


def _wire_messages(runtime: AgentRuntime) -> list[dict[str, Any]]:
    return _wire_messages_from(runtime.exchange.messages or runtime.state.messages)
