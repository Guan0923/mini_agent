"""DeepSeek request/response conversion isolated from the agent message model."""

from __future__ import annotations

import copy
import json
import math
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from mini_agent.domain import AssistantMessage, ChatMessage, SystemMessage, ToolMessage, ToolSpec, UserMessage
from mini_agent.runtime.core.context import AgentRuntime, PreparedResponse

from .config import ModelConfig
from .errors import (
    ModelConfigurationError,
    ModelRequestError,
    ModelTransportError,
    ProviderOutputError,
)


@dataclass(frozen=True)
class DeepSeekToolCall:
    id: str
    name: str
    arguments: str


@dataclass(frozen=True)
class DeepSeekUsage:
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None
    reasoning_tokens: int | None
    prompt_cache_hit_tokens: int | None = None
    prompt_cache_miss_tokens: int | None = None
    completion_tokens_details: dict[str, Any] | None = None


@dataclass
class DeepSeekCompletion(PreparedResponse):
    """Compatibility view over the provider-neutral prepared response."""

    @property
    def content(self) -> str | None:
        return self.message.content

    @property
    def reasoning_content(self) -> str | None:
        return self.message.reasoning

    @property
    def tool_calls(self) -> list[DeepSeekToolCall]:
        return [
            DeepSeekToolCall(tool.call_id, tool.name, json.dumps(tool.arguments, ensure_ascii=False))
            for tool in self.message.tool_messages
        ]


@dataclass(frozen=True)
class DeepSeekStreamDelta:
    content: str | None
    reasoning_content: str | None
    finish_reason: str | None = None
    role: str | None = None
    index: int = 0
    tool_calls: list[dict[str, Any]] | None = None
    logprobs: dict[str, Any] | None = None


_PROVIDER = "deepseek"
_TOOL_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_USER_ID = re.compile(r"^[A-Za-z0-9_-]{1,512}$")
_DOCUMENTED_PARAMETERS = {
    "frequency_penalty",
    "logprobs",
    "max_tokens",
    "presence_penalty",
    "reasoning_effort",
    "response_format",
    "stop",
    "stream_options",
    "temperature",
    "thinking",
    "tool_choice",
    "top_logprobs",
    "top_p",
    "user_id",
}
_MANAGED_PARAMETERS = {"messages", "model", "stream", "tools"}


def _provider_options(value: Any) -> dict[str, Any]:
    options = value.provider_options.get(_PROVIDER, {})
    if not isinstance(options, dict):
        raise ModelRequestError("DeepSeek provider_options must be an object.")
    return options


def _merge_extra_fields(
    target: dict[str, Any],
    raw: Any,
    *,
    protected: set[str],
    label: str,
) -> None:
    if raw is None:
        return
    if not isinstance(raw, Mapping):
        raise ModelRequestError(f"DeepSeek {label} extra_body must be an object.")
    collisions = protected.intersection(raw)
    if collisions:
        fields = ", ".join(sorted(collisions))
        raise ModelRequestError(f"DeepSeek {label} extra_body cannot override: {fields}.")
    target.update(copy.deepcopy(dict(raw)))


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


def _number(value: Any, *, name: str, minimum: float, maximum: float) -> int | float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise ModelRequestError(f"DeepSeek {name} must be a finite number.")
    if not minimum <= value <= maximum:
        raise ModelRequestError(f"DeepSeek {name} must be between {minimum:g} and {maximum:g}.")
    return value


def _validate_response_format(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping) or value.get("type") not in {"text", "json_object"}:
        raise ModelRequestError("DeepSeek response_format.type must be 'text' or 'json_object'.")
    return {"type": value["type"]}


def _validate_stop(value: Any) -> str | list[str]:
    if isinstance(value, str):
        return value
    if not isinstance(value, list) or len(value) > 16 or not all(isinstance(item, str) for item in value):
        raise ModelRequestError("DeepSeek stop must be text or a list of at most 16 strings.")
    return list(value)


def _validate_tool_choice(value: Any, tool_names: set[str]) -> str | dict[str, Any]:
    if isinstance(value, str):
        if value not in {"none", "auto", "required"}:
            raise ModelRequestError("DeepSeek tool_choice must be 'none', 'auto', 'required', or a named function.")
        if value in {"auto", "required"} and not tool_names:
            raise ModelRequestError(f"DeepSeek tool_choice={value!r} requires at least one tool.")
        return value
    try:
        choice_type = value["type"]
        name = value["function"]["name"]
    except (KeyError, TypeError) as exc:
        raise ModelRequestError("DeepSeek named tool_choice does not match the function schema.") from exc
    if choice_type != "function" or not isinstance(name, str) or name not in tool_names:
        raise ModelRequestError("DeepSeek named tool_choice must reference one of the supplied functions.")
    return {"type": "function", "function": {"name": name}}


def _validated_parameters(parameters: dict[str, Any]) -> dict[str, Any]:
    unknown = set(parameters) - _DOCUMENTED_PARAMETERS - _MANAGED_PARAMETERS - {"extra_body"}
    if unknown:
        fields = ", ".join(sorted(unknown))
        raise ModelRequestError(f"Unknown DeepSeek request parameter(s): {fields}; use extra_body for extensions.")
    managed = _MANAGED_PARAMETERS.intersection(parameters)
    if managed:
        fields = ", ".join(sorted(managed))
        raise ModelRequestError(f"DeepSeek request parameter(s) are managed by AgentRuntime: {fields}.")

    validated: dict[str, Any] = {}
    for name in ("frequency_penalty", "presence_penalty"):
        if name in parameters and parameters[name] is not None:
            value = parameters[name]
            if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
                raise ModelRequestError(f"DeepSeek deprecated {name} must still be a finite number.")
            validated[name] = value
    if parameters.get("max_tokens") is not None:
        value = parameters["max_tokens"]
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ModelRequestError("DeepSeek max_tokens must be a positive integer.")
        validated["max_tokens"] = value
    if parameters.get("temperature") is not None:
        validated["temperature"] = _number(parameters["temperature"], name="temperature", minimum=0, maximum=2)
    if parameters.get("top_p") is not None:
        validated["top_p"] = _number(parameters["top_p"], name="top_p", minimum=0, maximum=1)
    if parameters.get("thinking") is not None:
        value = parameters["thinking"]
        if not isinstance(value, Mapping) or value.get("type") not in {"enabled", "disabled"}:
            raise ModelRequestError("DeepSeek thinking.type must be 'enabled' or 'disabled'.")
        validated["thinking"] = copy.deepcopy(dict(value))
    if parameters.get("reasoning_effort") is not None:
        value = parameters["reasoning_effort"]
        if value not in {"low", "medium", "high", "xhigh", "max"}:
            raise ModelRequestError("DeepSeek reasoning_effort must be low, medium, high, xhigh, or max.")
        validated["reasoning_effort"] = value
    if parameters.get("response_format") is not None:
        validated["response_format"] = _validate_response_format(parameters["response_format"])
    if parameters.get("stop") is not None:
        validated["stop"] = _validate_stop(parameters["stop"])
    if parameters.get("logprobs") is not None:
        if not isinstance(parameters["logprobs"], bool):
            raise ModelRequestError("DeepSeek logprobs must be boolean.")
        validated["logprobs"] = parameters["logprobs"]
    if parameters.get("top_logprobs") is not None:
        value = parameters["top_logprobs"]
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 20:
            raise ModelRequestError("DeepSeek top_logprobs must be an integer between 0 and 20.")
        if parameters.get("logprobs") is not True:
            raise ModelRequestError("DeepSeek top_logprobs requires logprobs=true.")
        validated["top_logprobs"] = value
    if parameters.get("user_id") is not None:
        value = parameters["user_id"]
        if not isinstance(value, str) or not _USER_ID.fullmatch(value):
            raise ModelRequestError("DeepSeek user_id must contain 1-512 letters, digits, underscores, or hyphens.")
        validated["user_id"] = value
    if parameters.get("tool_choice") is not None:
        validated["tool_choice"] = parameters["tool_choice"]
    if parameters.get("stream_options") is not None:
        value = parameters["stream_options"]
        if not isinstance(value, Mapping):
            raise ModelRequestError("DeepSeek stream_options must be an object.")
        options = copy.deepcopy(dict(value))
        if "include_usage" in options and not isinstance(options["include_usage"], bool):
            raise ModelRequestError("DeepSeek stream_options.include_usage must be boolean.")
        validated["stream_options"] = options
    return validated


def _prepare_request(runtime: AgentRuntime) -> dict[str, Any]:
    """Convert provider-neutral runtime state into a DeepSeek request payload."""

    parameters = dict(runtime.state.request_parameters)
    overrides = runtime.exchange.context.get("request_parameters")
    if overrides is not None and not isinstance(overrides, Mapping):
        raise ModelRequestError("DeepSeek exchange request_parameters must be an object.")
    parameters.update(dict(overrides or {}))
    validated = _validated_parameters(parameters)
    if not isinstance(runtime.state.model, str) or not runtime.state.model:
        raise ModelRequestError("DeepSeek model must be a non-empty string.")
    payload: dict[str, Any] = {
        "model": runtime.state.model,
        "messages": _wire_messages(runtime),
        "stream": runtime.exchange.stream,
    }
    payload.update({key: value for key, value in validated.items() if key not in {"tool_choice", "stream_options"}})

    if runtime.exchange.output_mode == "json":
        if validated.get("response_format", {}).get("type") == "text":
            raise ModelRequestError("DeepSeek JSON output mode conflicts with response_format.type='text'.")
        payload["response_format"] = {"type": "json_object"}
        payload["thinking"] = {"type": "disabled"}
        payload.pop("reasoning_effort", None)
    tools = runtime.exchange.allowed_tools
    if runtime.exchange.output_mode == "tools" and not tools:
        raise ModelRequestError("DeepSeek tools output mode requires at least one allowed tool.")
    if len(tools) > 128:
        raise ModelRequestError("DeepSeek accepts at most 128 tools.")
    if tools:
        payload["tools"] = [_tool_definition(spec) for spec in tools]
    tool_names = {spec.name for spec in tools}
    if "tool_choice" in validated:
        payload["tool_choice"] = _validate_tool_choice(validated["tool_choice"], tool_names)
    elif tools:
        payload["tool_choice"] = "auto"
    if runtime.exchange.output_mode == "tools" and payload.get("tool_choice") == "none":
        raise ModelRequestError("DeepSeek tools output mode cannot use tool_choice='none'.")

    if runtime.exchange.stream:
        stream_options = validated.get("stream_options", {})
        stream_options.setdefault("include_usage", True)
        payload["stream_options"] = stream_options
    elif "stream_options" in validated:
        raise ModelRequestError("DeepSeek stream_options is only valid when stream=true.")

    _merge_extra_fields(
        payload,
        parameters.get("extra_body"),
        protected=_DOCUMENTED_PARAMETERS | _MANAGED_PARAMETERS | {"extra_body"},
        label="request",
    )

    runtime.exchange.request = payload
    return payload


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


def _prepare_response(runtime: AgentRuntime) -> PreparedResponse:
    """Convert a DeepSeek JSON response or SSE event iterator into one assistant message."""

    raw = runtime.exchange.raw_response
    if raw is None:
        raise ModelRequestError("DeepSeek response is missing from runtime.exchange.raw_response.")
    try:
        prepared = _parse_response(raw) if isinstance(raw, Mapping) else _parse_stream(runtime, raw)
    except ModelTransportError:
        raise
    except ModelRequestError as exc:
        invalid_output = ""
        if isinstance(raw, Mapping):
            invalid_output = json.dumps(raw, ensure_ascii=False, default=str)
        raise ProviderOutputError(
            str(exc),
            operation=runtime.exchange.operation,
            invalid_output=invalid_output,
            diagnostics=exc.diagnostics,
        ) from exc
    runtime.exchange.prepared_response = prepared
    runtime.state.turn_usage = prepared.usage
    return prepared


def _default_tokenizer_loader(identifier: str) -> Any:
    from tokenizers import Tokenizer

    return Tokenizer.from_pretrained(identifier)


class DeepSeek:
    """Convert runtime messages to and from the DeepSeek wire format."""

    def __init__(
        self,
        config: ModelConfig,
        tokenizer_loader: Callable[[str], Any] | None = None,
    ) -> None:
        self.config = config
        self._tokenizer_loader = tokenizer_loader or _default_tokenizer_loader
        self._tokenizer: Any | None = None

    @property
    def context_size(self) -> int:
        return self.config.context_size

    def estimate_tokens(
        self,
        messages: list[ChatMessage],
        tools: list[ToolSpec],
        request_parameters: dict[str, Any],
    ) -> int:
        payload: dict[str, Any] = {"messages": _wire_messages_from(messages)}
        if tools:
            payload["tools"] = [_tool_definition(spec) for spec in tools]
        serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        encoding = self._get_tokenizer().encode(serialized)
        max_tokens = request_parameters.get("max_tokens", self.config.max_tokens)
        if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens < 1:
            max_tokens = self.config.max_tokens
        return len(encoding.ids) + max_tokens

    def _get_tokenizer(self) -> Any:
        if self._tokenizer is not None:
            return self._tokenizer
        try:
            self._tokenizer = self._tokenizer_loader(self.config.tokenizer_model)
        except Exception as exc:
            raise ModelConfigurationError(
                "Unable to load tokenizer "
                f"{self.config.tokenizer_model!r}. Check network/cache access or set TOKENIZER_MODEL."
            ) from exc
        return self._tokenizer

    @property
    def endpoint(self) -> str:
        return self.config.endpoint

    @property
    def headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
        }

    @property
    def timeout_seconds(self) -> int:
        return self.config.timeout_seconds

    @property
    def operation(self) -> str:
        return "chat_completions"

    def prepare_request(self, runtime: AgentRuntime) -> dict[str, Any]:
        return _prepare_request(runtime)

    def prepare_response(self, runtime: AgentRuntime) -> PreparedResponse:
        return _prepare_response(runtime)
