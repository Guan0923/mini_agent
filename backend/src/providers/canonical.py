"""Provider adapters for canonical RuntimeState message content.

Only this module knows how a provider-neutral message becomes a wire request.
The inverse helpers intentionally discard provider metadata, headers and raw
transport objects before constructing a ``message`` node.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from backend.domain import CHECKPOINT_PREAMBLE
from backend.domain.runtime_state import (
    RuntimeState,
    RuntimeStateValidationError,
    message_payload,
)


def _payload(value: RuntimeState | Mapping[str, Any]) -> Mapping[str, Any]:
    if isinstance(value, RuntimeState):
        raise RuntimeStateValidationError("A Turn contains two messages; flatten it through _messages().")
    if not isinstance(value, Mapping) or value.get("role") not in {"user", "assistant"}:
        raise RuntimeStateValidationError("Provider adapters require a canonical Message object.")
    return value


def _messages(values: Iterable[RuntimeState | Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    ordered: list[RuntimeState | Mapping[str, Any]] = []
    positions: dict[tuple[str, str], int] = {}
    for value in values:
        raw_value = value.to_dict() if isinstance(value, RuntimeState) else value
        raw = raw_value if isinstance(raw_value, Mapping) else {}
        key = (
            (
                str(raw.get("session_id")),
                str(raw.get("id")),
            )
            if raw.get("session_id") and raw.get("id")
            else None
        )
        if key is not None and key in positions:
            ordered[positions[key]] = value
        else:
            if key is not None:
                positions[key] = len(ordered)
            ordered.append(value)

    result: list[Mapping[str, Any]] = []
    for value in ordered:
        if isinstance(value, RuntimeState):
            messages = value.selected_messages
        else:
            messages = [dict(value)]
        for message in messages:
            blocks = [dict(item) for item in message.get("content", []) if isinstance(item, Mapping)]
            rendered: list[dict[str, Any]] = []
            for block in blocks:
                if block.get("type") == "compaction":
                    rendered.append({"type": "text", "text": f"{CHECKPOINT_PREAMBLE}\n\n{block.get('summary', '')}"})
                elif block.get("type") == "error":
                    rendered.append({"type": "text", "text": str(block.get("message") or "Execution failed.")})
                else:
                    rendered.append(block)
            if message.get("role") == "assistant" and not rendered:
                continue
            result.append({**message, "content": rendered})
    return result


def model_parameters(value: RuntimeState | Mapping[str, Any]) -> dict[str, Any]:
    """Return request parameters from a node without leaking protocol fields."""

    raw = value.to_dict() if isinstance(value, RuntimeState) else value
    model = raw.get("model") if isinstance(raw, Mapping) else None
    if not isinstance(model, Mapping):
        return {}
    result = {
        "model": model.get("current_model"),
        "max_tokens": model.get("output_length"),
        "temperature": model.get("temperature"),
    }
    if model.get("thinking") != "disable":
        result["reasoning_effort"] = model.get("reasoning_effort")
    return {key: value for key, value in result.items() if value is not None}


def _is_message(value: RuntimeState | Mapping[str, Any]) -> bool:
    return isinstance(value, RuntimeState) or (
        isinstance(value, Mapping) and value.get("role") in {"user", "assistant"}
    )


def _status(value: RuntimeState | Mapping[str, Any]) -> str | None:
    if isinstance(value, RuntimeState):
        return value.status
    raw = value.get("status") if isinstance(value, Mapping) else None
    return raw if isinstance(raw, str) else None


def _text(blocks: Sequence[Mapping[str, Any]], *, include_reasoning: bool = False) -> str:
    parts: list[str] = []
    for block in blocks:
        kind = block.get("type")
        if kind == "text" or kind == "bash" or (include_reasoning and kind == "reasoning"):
            value = block.get("text")
            if isinstance(value, str):
                parts.append(value)
    return "".join(parts)


def _block_output(block: Mapping[str, Any]) -> str:
    if block.get("type") in {"text", "reasoning", "bash"}:
        return str(block.get("text") or "")
    if block.get("type") in {"tool_result", "approval", "question", "plan", "subagent", "skill_snapshot"}:
        value = block.get("content", block.get("result", block.get("text", "")))
        return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return json.dumps(dict(block), ensure_ascii=False)


def _chat_content(blocks: Sequence[Mapping[str, Any]]) -> str | list[dict[str, Any]] | None:
    """Return portable Chat Completions text content.

    Reasoning is emitted in the provider extension field by
    :func:`to_chat_completions`; putting a non-standard marker inside a text
    content part makes otherwise valid Chat Completions requests invalid.
    """

    rendered: list[dict[str, Any]] = []
    for block in blocks:
        kind = block.get("type")
        if kind == "text":
            rendered.append({"type": "text", "text": str(block.get("text") or "")})
        elif kind == "bash":
            rendered.append({"type": "text", "text": str(block.get("text") or "")})
    if not rendered:
        return None
    if len(rendered) == 1 and set(rendered[0]) == {"type", "text"}:
        return rendered[0]["text"]
    return rendered


def _has_chat_content(value: str | list[dict[str, Any]] | None) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, list):
        return any(
            isinstance(part.get("text"), str) and bool(part["text"].strip())
            for part in value
            if isinstance(part, Mapping)
        )
    return False


def to_chat_completions(values: Iterable[RuntimeState | Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Convert canonical message nodes to OpenAI Chat Completions messages."""

    output: list[dict[str, Any]] = []
    for message in _messages(values):
        role = str(message["role"])
        blocks = [dict(item) for item in message.get("content", []) if isinstance(item, Mapping)]
        if role == "tool_result":
            for block in blocks:
                if block.get("type") != "tool_result":
                    continue
                output.append(
                    {
                        "role": "tool",
                        "tool_call_id": str(block.get("call_id") or ""),
                        "content": _block_output(block),
                    }
                )
            continue
        wire_role = "user" if role in {"user", "bash"} else "assistant"
        item: dict[str, Any] = {"role": wire_role, "content": _chat_content(blocks)}
        reasoning = "".join(str(block.get("text") or "") for block in blocks if block.get("type") == "reasoning")
        if reasoning and wire_role == "assistant":
            item["reasoning_content"] = reasoning
        calls: list[dict[str, Any]] = []
        for block in blocks:
            if block.get("type") == "tool_call":
                calls.append(
                    {
                        "id": str(block.get("call_id") or ""),
                        "type": "function",
                        "function": {
                            "name": str(block.get("name") or ""),
                            "arguments": json.dumps(block.get("arguments", {}), ensure_ascii=False),
                        },
                    }
                )
        if calls:
            item["tool_calls"] = calls
            if item["content"] is None:
                item["content"] = ""
        elif wire_role == "assistant":
            content = item["content"]
            if not _has_chat_content(content):
                # Reasoning and control blocks are internal context, not a
                # valid standalone Chat Completions assistant message.
                continue
        output.append(item)
    return output


def to_responses(values: Iterable[RuntimeState | Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Convert canonical messages to OpenAI Responses input items."""

    output: list[dict[str, Any]] = []
    for message in _messages(values):
        role = str(message["role"])
        blocks = [dict(item) for item in message.get("content", []) if isinstance(item, Mapping)]
        if role == "tool_result":
            output.extend(
                {
                    "type": "function_call_output",
                    "call_id": str(block.get("call_id") or ""),
                    "output": _block_output(block),
                }
                for block in blocks
                if block.get("type") == "tool_result"
            )
            continue
        if role == "assistant":
            for block in blocks:
                kind = block.get("type")
                if kind == "tool_call":
                    output.append(
                        {
                            "type": "function_call",
                            "call_id": str(block.get("call_id") or ""),
                            "name": str(block.get("name") or ""),
                            "arguments": json.dumps(block.get("arguments", {}), ensure_ascii=False),
                        }
                    )
                elif kind == "reasoning":
                    output.append(
                        {"type": "reasoning", "summary": [{"type": "summary_text", "text": block.get("text", "")}]}
                    )
                elif kind in {"text", "bash"}:
                    output.append(
                        {
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": str(block.get("text") or "")}],
                        }
                    )
            continue
        input_role = "user" if role in {"user", "bash"} else role
        contents: list[dict[str, Any]] = []
        for block in blocks:
            if block.get("type") in {"text", "bash"}:
                contents.append({"type": "input_text", "text": str(block.get("text") or "")})
        output.append({"type": "message", "role": input_role, "content": contents})
    return output


def to_messages(values: Iterable[RuntimeState | Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Convert canonical messages to Anthropic Messages input items."""

    output: list[dict[str, Any]] = []
    for message in _messages(values):
        role = str(message["role"])
        blocks = [dict(item) for item in message.get("content", []) if isinstance(item, Mapping)]
        if role == "bash":
            role = "user"
        if role == "tool_result":
            role = "user"
        content: list[dict[str, Any]] = []
        for block in blocks:
            kind = block.get("type")
            if kind in {"text", "bash"}:
                content.append({"type": "text", "text": str(block.get("text") or "")})
            elif kind == "reasoning":
                content.append({"type": "thinking", "thinking": str(block.get("text") or "")})
            elif kind == "tool_call":
                content.append(
                    {
                        "type": "tool_use",
                        "id": str(block.get("call_id") or ""),
                        "name": str(block.get("name") or ""),
                        "input": dict(block.get("arguments") or {}),
                    }
                )
            elif kind == "tool_result":
                content.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": str(block.get("call_id") or ""),
                        "content": _block_output(block),
                        **({"is_error": True} if block.get("status") == "failed" else {}),
                    }
                )
        output.append({"role": role, "content": content})
    return output


def _assistant_payload(
    content: str | None, *, reasoning: str | None = None, tools: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    blocks: list[dict[str, Any]] = []
    if reasoning:
        blocks.append({"type": "reasoning", "text": reasoning})
    if content:
        blocks.append({"type": "text", "text": content})
    for index, tool in enumerate(tools or []):
        function = tool.get("function") if isinstance(tool.get("function"), Mapping) else {}
        arguments = function.get("arguments", {})
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                arguments = {"raw": arguments}
        blocks.append(
            {
                "type": "tool_call",
                "call_id": str(tool.get("id") or tool.get("call_id") or f"call_unknown_{index}"),
                "name": str(function.get("name") or tool.get("name") or "unknown"),
                "arguments": dict(arguments) if isinstance(arguments, Mapping) else {"value": arguments},
            }
        )
    return message_payload("assistant", blocks)


def from_chat_completion(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize one Chat Completions response choice into canonical data."""

    choices = payload.get("choices")
    choice = choices[0] if isinstance(choices, list) and choices else payload
    raw = choice.get("message", choice) if isinstance(choice, Mapping) else {}
    content = raw.get("content")
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        text = (
            "".join(
                str(item.get("text") or "")
                for item in content
                if isinstance(item, Mapping) and item.get("type") in {"text", "output_text"}
            )
            or None
        )
    else:
        text = None
    raw_reasoning = raw.get("reasoning_content", raw.get("reasoning"))
    if isinstance(raw_reasoning, str):
        reasoning = raw_reasoning
    elif isinstance(content, list):
        reasoning = (
            "".join(
                str(item.get("text") or "")
                for item in content
                if isinstance(item, Mapping) and item.get("type") in {"reasoning", "thinking"}
            )
            or None
        )
    else:
        reasoning = None
    tools = [dict(item) for item in raw.get("tool_calls", []) if isinstance(item, Mapping)]
    return _assistant_payload(text, reasoning=reasoning, tools=tools)


def from_responses(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize a Responses API output collection into canonical data."""

    blocks: list[dict[str, Any]] = []
    for index, item in enumerate(payload.get("output", []) if isinstance(payload.get("output"), list) else []):
        if not isinstance(item, Mapping):
            continue
        kind = item.get("type")
        if kind in {"message", "output_text"}:
            for content in item.get("content", []) if isinstance(item.get("content"), list) else [item]:
                if isinstance(content, Mapping) and content.get("type") in {"output_text", "text"}:
                    blocks.append({"type": "text", "text": str(content.get("text") or "")})
        elif kind == "reasoning":
            for summary in item.get("summary", []) if isinstance(item.get("summary"), list) else []:
                if isinstance(summary, Mapping):
                    blocks.append({"type": "reasoning", "text": str(summary.get("text") or "")})
        elif kind == "function_call":
            arguments = item.get("arguments", {})
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except json.JSONDecodeError:
                    arguments = {"raw": arguments}
            blocks.append(
                {
                    "type": "tool_call",
                    "call_id": str(item.get("call_id") or item.get("id") or f"call_unknown_{index}"),
                    "name": str(item.get("name") or "unknown"),
                    "arguments": dict(arguments) if isinstance(arguments, Mapping) else {"value": arguments},
                }
            )
    return message_payload("assistant", blocks)


def from_messages(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize an Anthropic Messages response into canonical data."""

    blocks: list[dict[str, Any]] = []
    for index, block in enumerate(payload.get("content", []) if isinstance(payload.get("content"), list) else []):
        if not isinstance(block, Mapping):
            continue
        kind = block.get("type")
        if kind == "text":
            blocks.append({"type": "text", "text": str(block.get("text") or "")})
        elif kind == "thinking":
            blocks.append({"type": "reasoning", "text": str(block.get("thinking") or "")})
        elif kind == "tool_use":
            raw_input = block.get("input")
            blocks.append(
                {
                    "type": "tool_call",
                    "call_id": str(block.get("id") or f"call_unknown_{index}"),
                    "name": str(block.get("name") or "unknown"),
                    "arguments": dict(raw_input) if isinstance(raw_input, Mapping) else {"value": raw_input},
                }
            )
    return message_payload("assistant", blocks)


def normalize_provider_response(provider: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    """Dispatch a provider response parser without retaining wire payload."""

    name = provider.lower()
    if name in {"openai", "chat_completions", "deepseek"}:
        return from_chat_completion(payload)
    if name in {"responses", "openai_responses"}:
        return from_responses(payload)
    if name in {"anthropic", "messages"}:
        return from_messages(payload)
    raise RuntimeStateValidationError(f"Unsupported provider response protocol: {provider!r}.")


class CanonicalProviderAdapter:
    """Protocol selector that never stores provider wire payloads."""

    def __init__(self, protocol: str) -> None:
        normalized = protocol.lower().replace("-", "_").replace(" ", "_")
        if normalized == "chat":
            normalized = "chat_completions"
        if normalized not in {"chat_completions", "responses", "messages"}:
            raise RuntimeStateValidationError(f"Unsupported provider protocol: {protocol!r}.")
        self.protocol = normalized

    def to_request(self, values: Iterable[RuntimeState | Mapping[str, Any]]) -> list[dict[str, Any]]:
        if self.protocol == "chat_completions":
            return to_chat_completions(values)
        if self.protocol == "responses":
            return to_responses(values)
        return to_messages(values)

    def from_response(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        if self.protocol == "chat_completions":
            return from_chat_completion(payload)
        if self.protocol == "responses":
            return from_responses(payload)
        return from_messages(payload)


__all__ = [
    "CanonicalProviderAdapter",
    "from_chat_completion",
    "from_messages",
    "from_responses",
    "normalize_provider_response",
    "to_chat_completions",
    "to_messages",
    "to_responses",
    "model_parameters",
]
