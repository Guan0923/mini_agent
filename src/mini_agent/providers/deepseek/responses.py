"""DeepSeek JSON and streaming response parsing."""

from __future__ import annotations

import copy
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from mini_agent.domain import AssistantMessage, ToolMessage
from mini_agent.runtime.core.context import AgentRuntime

from ..errors import ModelRequestError
from .common import _PROVIDER
from .models import DeepSeekCompletion


def _parse_arguments(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, str):
        raise ModelRequestError("DeepSeek tool-call arguments must be a JSON string.")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ModelRequestError("DeepSeek tool-call arguments are not valid JSON.") from exc
    if not isinstance(parsed, dict):
        raise ModelRequestError("DeepSeek tool-call arguments must decode to an object.")
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
        raise ModelRequestError("DeepSeek choice.message must be an object.")
    role = message.get("role")
    if role is not None and role != "assistant":
        raise ModelRequestError("DeepSeek response message role must be 'assistant'.")
    content = message.get("content")
    reasoning = message.get("reasoning_content")
    if content is not None and not isinstance(content, str):
        raise ModelRequestError("DeepSeek response content must be text or null.")
    if reasoning is not None and not isinstance(reasoning, str):
        raise ModelRequestError("DeepSeek reasoning_content must be text or null.")
    logprobs = raw_choice.get("logprobs")
    if logprobs is not None and not isinstance(logprobs, Mapping):
        raise ModelRequestError("DeepSeek logprobs must be an object or null.")
    index = raw_choice.get("index", position)
    if isinstance(index, bool) or not isinstance(index, int):
        raise ModelRequestError("DeepSeek choice index must be an integer.")
    finish_reason = raw_choice.get("finish_reason")
    if finish_reason is not None and not isinstance(finish_reason, str):
        raise ModelRequestError("DeepSeek finish_reason must be text or null.")

    raw_calls = message.get("tool_calls")
    if raw_calls is None:
        raw_calls = []
    if not isinstance(raw_calls, list):
        raise ModelRequestError("DeepSeek tool_calls must be an array.")
    tools: list[ToolMessage] = []
    for call in raw_calls:
        try:
            call_id = call["id"]
            call_type = call["type"]
            function = call["function"]
            name = function["name"]
            arguments = _parse_arguments(function["arguments"])
        except (KeyError, TypeError) as exc:
            raise ModelRequestError("DeepSeek tool call does not match the function-call schema.") from exc
        if call_type != "function":
            raise ModelRequestError("DeepSeek tool call type must be 'function'.")
        if not isinstance(call_id, str) or not call_id or not isinstance(name, str) or not name:
            raise ModelRequestError("DeepSeek tool call id and name must be non-empty strings.")
        if call_id in seen_call_ids:
            raise ModelRequestError(f"Duplicate DeepSeek tool call id in response: {call_id}.")
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


def _parse_response(data: Mapping[str, Any]) -> DeepSeekCompletion:
    raw_choices = data.get("choices")
    if not isinstance(raw_choices, list) or not raw_choices:
        raise ModelRequestError("DeepSeek response choices must be a non-empty array.")
    usage = data.get("usage")
    if usage is not None and not isinstance(usage, Mapping):
        raise ModelRequestError("DeepSeek usage must be an object or null.")
    response_metadata = _top_level_metadata(data)
    parsed: list[_ParsedChoice] = []
    seen_call_ids: set[str] = set()
    for position, choice in enumerate(raw_choices):
        if not isinstance(choice, Mapping):
            raise ModelRequestError("DeepSeek choices must contain objects.")
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
    return DeepSeekCompletion(
        message=primary.message,
        usage=copy.deepcopy(dict(usage)) if isinstance(usage, Mapping) else None,
        response_id=response_id if isinstance(response_id, str) else None,
        model=model if isinstance(model, str) else None,
        finish_reason=primary.finish_reason,
        provider_metadata=provider_metadata,
    )


def _merge_stream_logprobs(target: dict[str, Any], raw: Any) -> None:
    if raw is None:
        return
    if not isinstance(raw, Mapping):
        raise ModelRequestError("DeepSeek streamed logprobs must be an object or null.")
    for key, value in raw.items():
        if isinstance(value, list):
            existing = target.get(key)
            if not isinstance(existing, list):
                existing = []
                target[key] = existing
            existing.extend(copy.deepcopy(value))
        elif value is None:
            target.setdefault(key, None)
        else:
            target[key] = copy.deepcopy(value)


def _new_stream_choice(index: int) -> dict[str, Any]:
    return {
        "index": index,
        "role": None,
        "content": [],
        "reasoning": [],
        "tool_calls": {},
        "finish_reason": None,
        "logprobs": {},
    }


def _parse_stream(runtime: AgentRuntime, events: Iterable[dict[str, Any]]) -> DeepSeekCompletion:
    choices_by_index: dict[int, dict[str, Any]] = {}
    top: dict[str, Any] = {}
    usage: dict[str, Any] | None = None
    saw_event = False

    for raw_event in events:
        saw_event = True
        if not isinstance(raw_event, Mapping):
            raise ModelRequestError("DeepSeek stream events must be objects.")
        event = dict(raw_event)
        for key in ("id", "model", "object", "system_fingerprint", "created"):
            if key in event and event[key] is not None:
                top[key] = event[key]
        raw_usage = event.get("usage")
        if raw_usage is not None:
            if not isinstance(raw_usage, Mapping):
                raise ModelRequestError("DeepSeek streamed usage must be an object or null.")
            usage = copy.deepcopy(dict(raw_usage))
        raw_choices = event.get("choices")
        if not isinstance(raw_choices, list):
            raise ModelRequestError("DeepSeek stream choices must be an array.")
        for position, raw_choice in enumerate(raw_choices):
            if not isinstance(raw_choice, Mapping):
                raise ModelRequestError("DeepSeek stream choices must contain objects.")
            index = raw_choice.get("index", position)
            if isinstance(index, bool) or not isinstance(index, int):
                raise ModelRequestError("DeepSeek streamed choice index must be an integer.")
            target = choices_by_index.setdefault(index, _new_stream_choice(index))
            delta = raw_choice.get("delta")
            if delta is None:
                delta = {}
            if not isinstance(delta, Mapping):
                raise ModelRequestError("DeepSeek stream choice.delta must be an object.")
            role = delta.get("role")
            if role is not None:
                if role != "assistant":
                    raise ModelRequestError("DeepSeek streamed delta role must be 'assistant'.")
                target["role"] = role
            reasoning = delta.get("reasoning_content")
            if reasoning is not None:
                if not isinstance(reasoning, str):
                    raise ModelRequestError("DeepSeek stream reasoning_content must be text or null.")
                target["reasoning"].append(reasoning)
                if runtime.exchange.on_reasoning is not None:
                    runtime.exchange.on_reasoning(reasoning)
            content = delta.get("content")
            if content is not None:
                if not isinstance(content, str):
                    raise ModelRequestError("DeepSeek stream content must be text or null.")
                target["content"].append(content)
                if content and index == 0 and runtime.exchange.on_content is not None:
                    runtime.exchange.on_content(content)
            finish_reason = raw_choice.get("finish_reason")
            if finish_reason is not None:
                if not isinstance(finish_reason, str):
                    raise ModelRequestError("DeepSeek streamed finish_reason must be text or null.")
                target["finish_reason"] = finish_reason
            _merge_stream_logprobs(target["logprobs"], raw_choice.get("logprobs"))

            fragments = delta.get("tool_calls")
            if fragments is None:
                fragments = []
            if not isinstance(fragments, list):
                raise ModelRequestError("DeepSeek streamed tool_calls must be an array.")
            for fragment in fragments:
                if not isinstance(fragment, Mapping):
                    raise ModelRequestError("DeepSeek streamed tool call fragments must be objects.")
                tool_index = fragment.get("index")
                if isinstance(tool_index, bool) or not isinstance(tool_index, int):
                    raise ModelRequestError("DeepSeek streamed tool call is missing an integer index.")
                tool = target["tool_calls"].setdefault(
                    tool_index,
                    {"id": "", "type": "function", "name": "", "arguments": ""},
                )
                if fragment.get("type") is not None and fragment["type"] != "function":
                    raise ModelRequestError("DeepSeek streamed tool call type must be 'function'.")
                if isinstance(fragment.get("id"), str):
                    tool["id"] += fragment["id"]
                function = fragment.get("function")
                if function is None:
                    function = {}
                if not isinstance(function, Mapping):
                    raise ModelRequestError("DeepSeek streamed tool call function must be an object.")
                if isinstance(function.get("name"), str):
                    tool["name"] += function["name"]
                if isinstance(function.get("arguments"), str):
                    tool["arguments"] += function["arguments"]

    if not saw_event:
        raise ModelRequestError("DeepSeek stream ended without any events.")
    if not choices_by_index:
        raise ModelRequestError("DeepSeek stream ended without any completion choices.")
    unfinished = [index for index, choice in choices_by_index.items() if choice["finish_reason"] is None]
    if unfinished:
        indexes = ", ".join(str(index) for index in sorted(unfinished))
        raise ModelRequestError(f"DeepSeek stream ended without a finish reason for choice(s): {indexes}.")

    raw_choices: list[dict[str, Any]] = []
    for index in sorted(choices_by_index):
        choice = choices_by_index[index]
        tool_calls = [
            {
                "id": choice["tool_calls"][tool_index]["id"],
                "type": "function",
                "function": {
                    "name": choice["tool_calls"][tool_index]["name"],
                    "arguments": choice["tool_calls"][tool_index]["arguments"],
                },
            }
            for tool_index in sorted(choice["tool_calls"])
        ]
        raw_choices.append(
            {
                "index": index,
                "message": {
                    "role": choice["role"] or "assistant",
                    "content": "".join(choice["content"]) or None,
                    "reasoning_content": "".join(choice["reasoning"]) or None,
                    "tool_calls": tool_calls,
                },
                "finish_reason": choice["finish_reason"],
                "logprobs": choice["logprobs"] or None,
            }
        )
    return _parse_response({**top, "choices": raw_choices, "usage": usage})
