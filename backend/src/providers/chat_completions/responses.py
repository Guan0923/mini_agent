"""Chat Completions JSON and streaming response parsing."""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from backend.domain import AssistantMessage, ToolMessage, safe_error_message

from ..errors import ModelRequestError
from .common import _PROVIDER
from .models import ChatCompletion


def _parse_arguments(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, str):
        raise ModelRequestError("Chat Completions tool-call arguments must be a JSON string.")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ModelRequestError(safe_error_message(exc)) from exc
    if not isinstance(parsed, dict):
        raise ModelRequestError("Chat Completions tool-call arguments must decode to an object.")
    return parsed


def _top_level_metadata(data: Mapping[str, Any]) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    expected = {
        "created": int,
        "object": str,
        "system_fingerprint": str,
    }
    for key, value_type in expected.items():
        value = data.get(key)
        if isinstance(value, value_type) and not (value_type is int and isinstance(value, bool)):
            metadata[key] = value
    return metadata


@dataclass
class _ParsedChoice:
    index: int
    message: AssistantMessage
    finish_reason: str | None
    raw: dict[str, Any]


def _parse_choice(
    raw_choice: Mapping[str, Any],
    *,
    position: int,
    response_metadata: dict[str, Any],
    seen_call_ids: set[str],
) -> _ParsedChoice:
    message = raw_choice.get("message")
    if not isinstance(message, Mapping):
        raise ModelRequestError("Chat Completions choice.message must be an object.")
    role = message.get("role")
    if role is not None and role != "assistant":
        raise ModelRequestError("Chat Completions response message role must be 'assistant'.")
    content = message.get("content")
    reasoning = message.get("reasoning_content")
    if content is not None and not isinstance(content, str):
        raise ModelRequestError("Chat Completions response content must be text or null.")
    if reasoning is not None and not isinstance(reasoning, str):
        raise ModelRequestError("Chat Completions reasoning_content must be text or null.")
    logprobs = raw_choice.get("logprobs")
    if logprobs is not None and not isinstance(logprobs, Mapping):
        raise ModelRequestError("Chat Completions logprobs must be an object or null.")
    index = raw_choice.get("index", position)
    if isinstance(index, bool) or not isinstance(index, int):
        raise ModelRequestError("Chat Completions choice index must be an integer.")
    finish_reason = raw_choice.get("finish_reason")
    if finish_reason is not None and not isinstance(finish_reason, str):
        raise ModelRequestError("Chat Completions finish_reason must be text or null.")

    raw_calls = message.get("tool_calls")
    if raw_calls is None:
        raw_calls = []
    if not isinstance(raw_calls, list):
        raise ModelRequestError("Chat Completions tool_calls must be an array.")
    tools: list[ToolMessage] = []
    for call in raw_calls:
        try:
            call_id = call["id"]
            call_type = call["type"]
            function = call["function"]
            name = function["name"]
            arguments = _parse_arguments(function["arguments"])
        except (KeyError, TypeError) as exc:
            raise ModelRequestError(safe_error_message(exc)) from exc
        if call_type != "function":
            raise ModelRequestError("Chat Completions tool call type must be 'function'.")
        if not isinstance(call_id, str) or not call_id or not isinstance(name, str) or not name:
            raise ModelRequestError("Chat Completions tool call id and name must be non-empty strings.")
        if call_id in seen_call_ids:
            raise ModelRequestError(f"Duplicate Chat Completions tool call id in response: {call_id}.")
        seen_call_ids.add(call_id)
        tools.append(ToolMessage(name=name, call_id=call_id, arguments=arguments))

    native_response = copy.deepcopy(response_metadata)
    native_response["choice_index"] = index
    assistant = AssistantMessage(
        content=content,
        reasoning=reasoning,
        logprobs=copy.deepcopy(dict(logprobs)) if isinstance(logprobs, Mapping) else None,
        tool_messages=tools,
        provider_options={_PROVIDER: {"response": native_response}},
    )
    return _ParsedChoice(index, assistant, finish_reason, copy.deepcopy(dict(raw_choice)))


def _parse_response(data: Mapping[str, Any]) -> ChatCompletion:
    raw_choices = data.get("choices")
    if not isinstance(raw_choices, list) or not raw_choices:
        raise ModelRequestError("Chat Completions response choices must be a non-empty array.")
    usage = data.get("usage")
    if usage is not None and not isinstance(usage, Mapping):
        raise ModelRequestError("Chat Completions usage must be an object or null.")
    response_metadata = _top_level_metadata(data)
    parsed: list[_ParsedChoice] = []
    seen_call_ids: set[str] = set()
    for position, choice in enumerate(raw_choices):
        if not isinstance(choice, Mapping):
            raise ModelRequestError("Chat Completions choices must contain objects.")
        parsed.append(
            _parse_choice(
                choice,
                position=position,
                response_metadata=response_metadata,
                seen_call_ids=seen_call_ids,
            )
        )
    parsed.sort(key=lambda choice: choice.index)
    primary = parsed[0]
    if len(parsed) > 1:
        primary.message.provider_options[_PROVIDER]["response"]["alternative_choices"] = [
            choice.raw for choice in parsed[1:]
        ]

    provider_metadata = copy.deepcopy(response_metadata)
    provider_metadata["choices"] = [choice.raw for choice in parsed]
    response_id = data.get("id")
    model = data.get("model")
    return ChatCompletion(
        message=primary.message,
        usage=copy.deepcopy(dict(usage)) if isinstance(usage, Mapping) else None,
        response_id=response_id if isinstance(response_id, str) else None,
        model=model if isinstance(model, str) else None,
        finish_reason=primary.finish_reason,
        provider_metadata=provider_metadata,
    )
