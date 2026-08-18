"""Chat Completions streaming response accumulation."""

from __future__ import annotations

import copy
from collections.abc import Iterable, Mapping
from typing import Any

from backend.runtime.core.context import AgentRuntime

from ..errors import ModelRequestError
from .models import ChatCompletion
from .responses import _parse_response


def _merge_stream_logprobs(target: dict[str, Any], raw: Any) -> None:
    if raw is None:
        return
    if not isinstance(raw, Mapping):
        raise ModelRequestError("Chat Completions streamed logprobs must be an object or null.")
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


def _parse_stream(runtime: AgentRuntime, events: Iterable[dict[str, Any]]) -> ChatCompletion:
    choices_by_index: dict[int, dict[str, Any]] = {}
    top: dict[str, Any] = {}
    usage: dict[str, Any] | None = None
    saw_event = False

    for raw_event in events:
        saw_event = True
        if not isinstance(raw_event, Mapping):
            raise ModelRequestError("Chat Completions stream events must be objects.")
        event = dict(raw_event)
        for key in ("id", "model", "object", "system_fingerprint", "created"):
            if key in event and event[key] is not None:
                top[key] = event[key]
        raw_usage = event.get("usage")
        if raw_usage is not None:
            if not isinstance(raw_usage, Mapping):
                raise ModelRequestError("Chat Completions streamed usage must be an object or null.")
            usage = copy.deepcopy(dict(raw_usage))
        raw_choices = event.get("choices")
        if not isinstance(raw_choices, list):
            raise ModelRequestError("Chat Completions stream choices must be an array.")
        for position, raw_choice in enumerate(raw_choices):
            if not isinstance(raw_choice, Mapping):
                raise ModelRequestError("Chat Completions stream choices must contain objects.")
            index = raw_choice.get("index", position)
            if isinstance(index, bool) or not isinstance(index, int):
                raise ModelRequestError("Chat Completions streamed choice index must be an integer.")
            target = choices_by_index.setdefault(index, _new_stream_choice(index))
            delta = raw_choice.get("delta")
            if delta is None:
                delta = {}
            if not isinstance(delta, Mapping):
                raise ModelRequestError("Chat Completions stream choice.delta must be an object.")
            role = delta.get("role")
            if role is not None:
                if role != "assistant":
                    raise ModelRequestError("Chat Completions streamed delta role must be 'assistant'.")
                target["role"] = role
            reasoning = delta.get("reasoning_content")
            if reasoning is not None:
                if not isinstance(reasoning, str):
                    raise ModelRequestError("Chat Completions stream reasoning_content must be text or null.")
                target["reasoning"].append(reasoning)
                if runtime.exchange.on_reasoning is not None:
                    runtime.exchange.on_reasoning(reasoning)
            content = delta.get("content")
            if content is not None:
                if not isinstance(content, str):
                    raise ModelRequestError("Chat Completions stream content must be text or null.")
                target["content"].append(content)
                if content and index == 0 and runtime.exchange.on_content is not None:
                    runtime.exchange.on_content(content)
            finish_reason = raw_choice.get("finish_reason")
            if finish_reason is not None:
                if not isinstance(finish_reason, str):
                    raise ModelRequestError("Chat Completions streamed finish_reason must be text or null.")
                target["finish_reason"] = finish_reason
            _merge_stream_logprobs(target["logprobs"], raw_choice.get("logprobs"))

            fragments = delta.get("tool_calls")
            if fragments is None:
                fragments = []
            if not isinstance(fragments, list):
                raise ModelRequestError("Chat Completions streamed tool_calls must be an array.")
            for fragment in fragments:
                if not isinstance(fragment, Mapping):
                    raise ModelRequestError("Chat Completions streamed tool call fragments must be objects.")
                tool_index = fragment.get("index")
                if isinstance(tool_index, bool) or not isinstance(tool_index, int):
                    raise ModelRequestError("Chat Completions streamed tool call is missing an integer index.")
                tool = target["tool_calls"].setdefault(
                    tool_index,
                    {"id": "", "type": "function", "name": "", "arguments": ""},
                )
                if fragment.get("type") is not None and fragment["type"] != "function":
                    raise ModelRequestError("Chat Completions streamed tool call type must be 'function'.")
                if isinstance(fragment.get("id"), str):
                    tool["id"] += fragment["id"]
                function = fragment.get("function")
                if function is None:
                    function = {}
                if not isinstance(function, Mapping):
                    raise ModelRequestError("Chat Completions streamed tool call function must be an object.")
                if isinstance(function.get("name"), str):
                    tool["name"] += function["name"]
                if isinstance(function.get("arguments"), str):
                    tool["arguments"] += function["arguments"]

    if not saw_event:
        raise ModelRequestError("Chat Completions stream ended without any events.")
    if not choices_by_index:
        raise ModelRequestError("Chat Completions stream ended without any completion choices.")
    unfinished = [index for index, choice in choices_by_index.items() if choice["finish_reason"] is None]
    if unfinished:
        indexes = ", ".join(str(index) for index in sorted(unfinished))
        raise ModelRequestError(f"Chat Completions stream ended without a finish reason for choice(s): {indexes}.")

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
