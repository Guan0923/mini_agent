"""Chat Completions request parameter validation and payload construction."""

from __future__ import annotations

import copy
import math
from collections.abc import Mapping
from typing import Any

from backend.domain import safe_error_message
from backend.runtime.core.context import AgentRuntime

from ..errors import ModelRequestError
from .common import (
    _DOCUMENTED_PARAMETERS,
    _MANAGED_PARAMETERS,
    _USER_ID,
    _merge_extra_fields,
)
from .messages import _tool_definition, _wire_messages


def _number(value: Any, *, name: str, minimum: float, maximum: float) -> int | float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise ModelRequestError(f"Chat Completions {name} must be a finite number.")
    if not minimum <= value <= maximum:
        raise ModelRequestError(f"Chat Completions {name} must be between {minimum:g} and {maximum:g}.")
    return value


def _validate_response_format(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping) or value.get("type") not in {"text", "json_object"}:
        raise ModelRequestError("Chat Completions response_format.type must be 'text' or 'json_object'.")
    return {"type": value["type"]}


def _validate_stop(value: Any) -> str | list[str]:
    if isinstance(value, str):
        return value
    if not isinstance(value, list) or len(value) > 16 or not all(isinstance(item, str) for item in value):
        raise ModelRequestError("Chat Completions stop must be text or a list of at most 16 strings.")
    return list(value)


def _validate_tool_choice(value: Any, tool_names: set[str]) -> str | dict[str, Any]:
    if isinstance(value, str):
        if value not in {"none", "auto", "required"}:
            raise ModelRequestError(
                "Chat Completions tool_choice must be 'none', 'auto', 'required', or a named function."
            )
        if value in {"auto", "required"} and not tool_names:
            raise ModelRequestError(f"Chat Completions tool_choice={value!r} requires at least one tool.")
        return value
    try:
        choice_type = value["type"]
        name = value["function"]["name"]
    except (KeyError, TypeError) as exc:
        raise ModelRequestError(safe_error_message(exc)) from exc
    if choice_type != "function" or not isinstance(name, str) or name not in tool_names:
        raise ModelRequestError("Chat Completions named tool_choice must reference one of the supplied functions.")
    return {"type": "function", "function": {"name": name}}


def _validated_parameters(parameters: dict[str, Any]) -> dict[str, Any]:
    unknown = set(parameters) - _DOCUMENTED_PARAMETERS - _MANAGED_PARAMETERS - {"extra_body"}
    if unknown:
        fields = ", ".join(sorted(unknown))
        raise ModelRequestError(
            f"Unknown Chat Completions request parameter(s): {fields}; use extra_body for extensions."
        )
    managed = _MANAGED_PARAMETERS.intersection(parameters)
    if managed:
        fields = ", ".join(sorted(managed))
        raise ModelRequestError(f"Chat Completions request parameter(s) are managed by AgentRuntime: {fields}.")

    validated: dict[str, Any] = {}
    for name in ("frequency_penalty", "presence_penalty"):
        if name in parameters and parameters[name] is not None:
            value = parameters[name]
            if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
                raise ModelRequestError(f"Chat Completions deprecated {name} must still be a finite number.")
            validated[name] = value
    if parameters.get("max_tokens") is not None:
        value = parameters["max_tokens"]
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ModelRequestError("Chat Completions max_tokens must be a positive integer.")
        validated["max_tokens"] = value
    if parameters.get("temperature") is not None:
        validated["temperature"] = _number(parameters["temperature"], name="temperature", minimum=0, maximum=2)
    if parameters.get("top_p") is not None:
        validated["top_p"] = _number(parameters["top_p"], name="top_p", minimum=0, maximum=1)
    if parameters.get("thinking") is not None:
        value = parameters["thinking"]
        if not isinstance(value, Mapping) or value.get("type") not in {"enabled", "disabled"}:
            raise ModelRequestError("Chat Completions thinking.type must be 'enabled' or 'disabled'.")
        validated["thinking"] = copy.deepcopy(dict(value))
    if parameters.get("reasoning_effort") is not None:
        value = parameters["reasoning_effort"]
        if value not in {"low", "medium", "high", "xhigh", "max"}:
            raise ModelRequestError("Chat Completions reasoning_effort must be low, medium, high, xhigh, or max.")
        validated["reasoning_effort"] = value
    if parameters.get("response_format") is not None:
        validated["response_format"] = _validate_response_format(parameters["response_format"])
    if parameters.get("stop") is not None:
        validated["stop"] = _validate_stop(parameters["stop"])
    if parameters.get("logprobs") is not None:
        if not isinstance(parameters["logprobs"], bool):
            raise ModelRequestError("Chat Completions logprobs must be boolean.")
        validated["logprobs"] = parameters["logprobs"]
    if parameters.get("top_logprobs") is not None:
        value = parameters["top_logprobs"]
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 20:
            raise ModelRequestError("Chat Completions top_logprobs must be an integer between 0 and 20.")
        if parameters.get("logprobs") is not True:
            raise ModelRequestError("Chat Completions top_logprobs requires logprobs=true.")
        validated["top_logprobs"] = value
    if parameters.get("user_id") is not None:
        value = parameters["user_id"]
        if not isinstance(value, str) or not _USER_ID.fullmatch(value):
            raise ModelRequestError(
                "Chat Completions user_id must contain 1-512 letters, digits, underscores, or hyphens."
            )
        validated["user_id"] = value
    if parameters.get("tool_choice") is not None:
        validated["tool_choice"] = parameters["tool_choice"]
    if parameters.get("stream_options") is not None:
        value = parameters["stream_options"]
        if not isinstance(value, Mapping):
            raise ModelRequestError("Chat Completions stream_options must be an object.")
        options = copy.deepcopy(dict(value))
        if "include_usage" in options and not isinstance(options["include_usage"], bool):
            raise ModelRequestError("Chat Completions stream_options.include_usage must be boolean.")
        validated["stream_options"] = options
    return validated


def _prepare_request(runtime: AgentRuntime) -> dict[str, Any]:
    """Convert provider-neutral runtime state into a Chat Completions payload."""

    config = runtime.request_config()
    parameters = dict(config.get("request_parameters") or {})
    overrides = runtime.exchange.context.get("request_parameters")
    if overrides is not None and not isinstance(overrides, Mapping):
        raise ModelRequestError("Chat Completions exchange request_parameters must be an object.")
    parameters.update(dict(overrides or {}))
    required_tool_name = parameters.pop("required_tool_name", None)
    # The active dynamic node is the authoritative request configuration.
    snapshot = config.get("model_snapshot") or {}
    if isinstance(snapshot, Mapping):
        if snapshot.get("output_length") is not None:
            parameters["max_tokens"] = snapshot.get("output_length")
        if snapshot.get("temperature") is not None:
            parameters["temperature"] = snapshot.get("temperature")
        if snapshot.get("thinking") is not None:
            parameters["thinking"] = {"type": "enabled" if snapshot.get("thinking") == "enable" else "disabled"}
        if snapshot.get("thinking") != "disable" and snapshot.get("reasoning_effort") is not None:
            # A runtime snapshot may be incomplete while older callers still
            # provide the documented request parameter explicitly.  Do not
            # replace that value with ``None`` merely because the snapshot has
            # not been populated yet.
            parameters["reasoning_effort"] = snapshot.get("reasoning_effort")
        elif snapshot.get("thinking") == "disable":
            parameters.pop("reasoning_effort", None)
    validated = _validated_parameters(parameters)
    model = config.get("model") or snapshot.get("current_model")
    if not isinstance(model, str) or not model:
        raise ModelRequestError("Chat Completions model must be a non-empty string.")
    payload: dict[str, Any] = {
        "model": model,
        "messages": _wire_messages(runtime),
        "stream": runtime.exchange.stream,
    }
    payload.update({key: value for key, value in validated.items() if key not in {"tool_choice", "stream_options"}})

    if runtime.exchange.output_mode == "json":
        if validated.get("response_format", {}).get("type") == "text":
            raise ModelRequestError("Chat Completions JSON output mode conflicts with response_format.type='text'.")
        payload["response_format"] = {"type": "json_object"}
        payload["thinking"] = {"type": "disabled"}
        payload.pop("reasoning_effort", None)
    tools = runtime.exchange.allowed_tools
    if runtime.exchange.output_mode == "tools" and not tools:
        raise ModelRequestError("Chat Completions tools output mode requires at least one allowed tool.")
    if len(tools) > 128:
        raise ModelRequestError("Chat Completions accepts at most 128 tools.")
    if tools:
        payload["tools"] = [_tool_definition(spec) for spec in tools]
    tool_names = {spec.name for spec in tools}
    if "tool_choice" in validated:
        payload["tool_choice"] = _validate_tool_choice(validated["tool_choice"], tool_names)
    elif isinstance(required_tool_name, str) and required_tool_name:
        payload["tool_choice"] = _validate_tool_choice(
            {"type": "function", "function": {"name": required_tool_name}}, tool_names
        )
    elif tools:
        payload["tool_choice"] = "auto"
    if runtime.exchange.output_mode == "tools" and payload.get("tool_choice") == "none":
        raise ModelRequestError("Chat Completions tools output mode cannot use tool_choice='none'.")

    if runtime.exchange.stream:
        stream_options = validated.get("stream_options", {})
        stream_options.setdefault("include_usage", True)
        payload["stream_options"] = stream_options
    elif "stream_options" in validated:
        raise ModelRequestError("Chat Completions stream_options is only valid when stream=true.")

    _merge_extra_fields(
        payload,
        parameters.get("extra_body"),
        protected=_DOCUMENTED_PARAMETERS | _MANAGED_PARAMETERS | {"extra_body"},
        label="request",
    )

    runtime.exchange.request = payload
    return payload
