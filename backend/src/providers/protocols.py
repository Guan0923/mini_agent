"""Provider-neutral adapters for Chat Completions, Responses, and Messages."""

from __future__ import annotations

import copy
import json
from collections.abc import Iterable, Mapping
from typing import Any

from backend.domain import AssistantMessage, ChatMessage, SystemMessage, ToolMessage, ToolSpec, UserMessage
from backend.runtime.core.context import AgentRuntime, PreparedResponse

from .chat_completions import ChatCompletions
from .config import ModelConfig
from .errors import ModelRequestError, ProviderOutputError


class ChatCompletionsAdapter(ChatCompletions):
    """OpenAI-compatible Chat Completions adapter."""

    pass


def _text_content(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, Mapping) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "".join(parts)
    return ""


def _estimate(messages: list[ChatMessage], tools: list[ToolSpec], parameters: Mapping[str, Any]) -> int:
    payload = {
        "messages": [getattr(message, "content", "") or "" for message in messages],
        "tools": [{"name": spec.name, "parameters": spec.parameters} for spec in tools],
    }
    input_tokens = max(1, len(json.dumps(payload, ensure_ascii=False)) // 4)
    max_tokens = parameters.get("max_tokens", 0)
    return input_tokens + (int(max_tokens) if isinstance(max_tokens, int) and max_tokens > 0 else 0)


class ResponsesAdapter:
    def __init__(self, config: ModelConfig) -> None:
        self.config = config

    @property
    def context_size(self) -> int:
        return self.config.context_size

    @property
    def endpoint(self) -> str:
        return self.config.endpoint

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.config.api_key}", "Content-Type": "application/json"}

    @property
    def timeout_seconds(self) -> int:
        return self.config.timeout_seconds

    @property
    def operation(self) -> str:
        return "responses"

    def estimate_tokens(self, messages, tools, request_parameters):
        return _estimate(messages, tools, request_parameters)

    estimate_input_tokens = estimate_tokens

    def prepare_request(self, runtime: AgentRuntime) -> dict[str, Any]:
        config = runtime.request_config()
        parameters = dict(config.get("request_parameters") or {})
        overrides = runtime.exchange.context.get("request_parameters")
        if isinstance(overrides, Mapping):
            parameters.update(overrides)
        required_tool_name = parameters.get("required_tool_name")
        items: list[dict[str, Any]] = []
        for message in runtime.exchange.messages or runtime.state.messages:
            if isinstance(message, SystemMessage | UserMessage):
                items.append({"role": message.role, "content": message.content or ""})
            elif isinstance(message, AssistantMessage):
                if message.content:
                    items.append({"role": "assistant", "content": message.content})
                for tool in message.tool_messages:
                    items.append(
                        {
                            "type": "function_call",
                            "id": tool.call_id,
                            "call_id": tool.call_id,
                            "name": tool.name,
                            "arguments": json.dumps(tool.arguments, ensure_ascii=False, separators=(",", ":")),
                        }
                    )
                    if tool.status != "pending" and tool.content is not None:
                        items.append(
                            {
                                "type": "function_call_output",
                                "call_id": tool.call_id,
                                "output": tool.content,
                            }
                        )
        model_snapshot = config.get("model_snapshot") if isinstance(config.get("model_snapshot"), Mapping) else {}
        payload: dict[str, Any] = {
            "model": str(config.get("model") or model_snapshot.get("current_model") or self.config.model),
            "input": items,
            "stream": runtime.exchange.stream,
            "max_output_tokens": int(
                parameters.get("max_tokens", model_snapshot.get("output_length", self.config.max_tokens))
            ),
        }
        temperature = parameters.get("temperature", model_snapshot.get("temperature", self.config.temperature))
        if temperature is not None:
            payload["temperature"] = temperature
        if parameters.get("reasoning_effort") is not None:
            payload["reasoning"] = {"effort": parameters["reasoning_effort"]}
        if parameters.get("thinking") == {"type": "disabled"}:
            payload.pop("reasoning", None)
        tools = runtime.exchange.allowed_tools
        if tools:
            payload["tools"] = [
                {"type": "function", "name": spec.name, "description": spec.description, "parameters": spec.parameters}
                for spec in tools
            ]
        if isinstance(required_tool_name, str) and required_tool_name:
            payload["tool_choice"] = {"type": "function", "name": required_tool_name}
        if runtime.exchange.output_mode == "json":
            payload["text"] = {"format": {"type": "json_object"}}
        runtime.exchange.request = payload
        return payload

    def prepare_response(self, runtime: AgentRuntime) -> PreparedResponse:
        raw = runtime.exchange.raw_response
        try:
            parsed = self._parse_stream(runtime, raw) if not isinstance(raw, Mapping) else self._parse_json(raw)
        except ModelRequestError as exc:
            raise ProviderOutputError(str(exc), operation=runtime.exchange.operation) from exc
        runtime.exchange.prepared_response = parsed
        runtime.state.turn_usage = parsed.usage
        return parsed

    def _parse_json(self, data: Mapping[str, Any]) -> PreparedResponse:
        output = data.get("output")
        if not isinstance(output, list):
            raise ModelRequestError("Responses output must be an array.")
        content = ""
        reasoning = ""
        tools: list[ToolMessage] = []
        for item in output:
            if not isinstance(item, Mapping):
                continue
            item_type = item.get("type")
            if item_type == "message":
                content += _text_content(item.get("content"))
            elif item_type in {"reasoning", "summary"}:
                reasoning += _text_content(item.get("summary") or item.get("content"))
            elif item_type == "function_call":
                arguments = item.get("arguments", "{}")
                try:
                    parsed_arguments = json.loads(arguments) if isinstance(arguments, str) else dict(arguments)
                except (TypeError, ValueError) as exc:
                    raise ModelRequestError("Responses function call arguments are invalid JSON.") from exc
                tools.append(
                    ToolMessage(
                        name=str(item.get("name") or ""),
                        call_id=str(item.get("call_id") or item.get("id") or ""),
                        arguments=parsed_arguments,
                        status="pending",
                    )
                )
        usage = data.get("usage")
        usage = dict(usage) if isinstance(usage, Mapping) else None
        return PreparedResponse(
            AssistantMessage(
                content=content or None,
                reasoning=reasoning or None,
                tool_messages=tools,
                provider_options={"responses": {"response": copy.deepcopy(dict(data))}},
            ),
            usage=usage,
            response_id=str(data.get("id")) if data.get("id") else None,
            model=str(data.get("model")) if data.get("model") else None,
            finish_reason=str(data.get("status")) if data.get("status") else None,
            provider_metadata={"type": data.get("object", "response")},
        )

    def _parse_stream(self, runtime: AgentRuntime, events: Iterable[dict[str, Any]]) -> PreparedResponse:
        text: list[str] = []
        reasoning: list[str] = []
        calls: dict[str, dict[str, Any]] = {}
        usage: dict[str, Any] | None = None
        final: Mapping[str, Any] | None = None
        for event in events:
            kind = str(event.get("__sse_event") or event.get("type") or "")
            if kind == "response.output_text.delta":
                delta = event.get("delta", "")
                if isinstance(delta, str):
                    text.append(delta)
                    if runtime.exchange.on_content:
                        runtime.exchange.on_content(delta)
            elif kind in {"response.reasoning_text.delta", "response.reasoning_summary_text.delta"}:
                delta = event.get("delta", "")
                if isinstance(delta, str):
                    reasoning.append(delta)
                    if runtime.exchange.on_reasoning:
                        runtime.exchange.on_reasoning(delta)
            elif kind == "response.output_item.added":
                item = event.get("item")
                if isinstance(item, Mapping) and item.get("type") == "function_call":
                    key = str(item.get("call_id") or item.get("id") or len(calls))
                    calls[key] = {
                        "name": str(item.get("name") or ""),
                        "call_id": key,
                        "arguments": str(item.get("arguments") or ""),
                    }
            elif kind == "response.function_call_arguments.delta":
                key = str(event.get("call_id") or event.get("item_id") or "")
                target = calls.setdefault(key, {"name": str(event.get("name") or ""), "call_id": key, "arguments": ""})
                if isinstance(event.get("delta"), str):
                    target["arguments"] += event["delta"]
            elif kind == "response.completed":
                candidate = event.get("response")
                if isinstance(candidate, Mapping):
                    final = candidate
                    raw_usage = candidate.get("usage")
                    if isinstance(raw_usage, Mapping):
                        usage = dict(raw_usage)
        if final is not None:
            parsed = self._parse_json(final)
            if text:
                parsed.message.content = "".join(text)
            if reasoning:
                parsed.message.reasoning = "".join(reasoning)
            return parsed
        tools = []
        for value in calls.values():
            try:
                arguments = json.loads(value["arguments"] or "{}")
            except ValueError as exc:
                raise ModelRequestError("Responses streamed function arguments are invalid JSON.") from exc
            tools.append(
                ToolMessage(name=value["name"], call_id=value["call_id"], arguments=arguments, status="pending")
            )
        return PreparedResponse(
            AssistantMessage(content="".join(text) or None, reasoning="".join(reasoning) or None, tool_messages=tools),
            usage=usage,
        )


class MessagesAdapter:
    def __init__(self, config: ModelConfig) -> None:
        self.config = config

    @property
    def context_size(self) -> int:
        return self.config.context_size

    @property
    def endpoint(self) -> str:
        return self.config.endpoint

    @property
    def headers(self) -> dict[str, str]:
        return {
            "x-api-key": self.config.api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }

    @property
    def timeout_seconds(self) -> int:
        return self.config.timeout_seconds

    @property
    def operation(self) -> str:
        return "messages"

    def estimate_tokens(self, messages, tools, request_parameters):
        return _estimate(messages, tools, request_parameters)

    estimate_input_tokens = estimate_tokens

    def prepare_request(self, runtime: AgentRuntime) -> dict[str, Any]:
        config = runtime.request_config()
        parameters = dict(config.get("request_parameters") or {})
        overrides = runtime.exchange.context.get("request_parameters")
        if isinstance(overrides, Mapping):
            parameters.update(overrides)
        required_tool_name = parameters.get("required_tool_name")
        system: list[dict[str, Any]] = []
        messages: list[dict[str, Any]] = []
        for message in runtime.exchange.messages or runtime.state.messages:
            if isinstance(message, SystemMessage):
                system.append({"type": "text", "text": message.content or ""})
            elif isinstance(message, UserMessage):
                messages.append({"role": "user", "content": [{"type": "text", "text": message.content or ""}]})
            elif isinstance(message, AssistantMessage):
                blocks: list[dict[str, Any]] = []
                results: list[dict[str, Any]] = []
                if message.content:
                    blocks.append({"type": "text", "text": message.content})
                for tool in message.tool_messages:
                    blocks.append({"type": "tool_use", "id": tool.call_id, "name": tool.name, "input": tool.arguments})
                    if tool.status != "pending" and tool.content is not None:
                        results.append(
                            {
                                "type": "tool_result",
                                "tool_use_id": tool.call_id,
                                "content": tool.content,
                                **({"is_error": True} if tool.status == "failed" else {}),
                            }
                        )
                if blocks:
                    messages.append({"role": "assistant", "content": blocks})
                if results:
                    messages.append({"role": "user", "content": results})
        model_snapshot = config.get("model_snapshot") if isinstance(config.get("model_snapshot"), Mapping) else {}
        payload: dict[str, Any] = {
            "model": str(config.get("model") or model_snapshot.get("current_model") or self.config.model),
            "messages": messages,
            "max_tokens": int(
                parameters.get("max_tokens", model_snapshot.get("output_length", self.config.max_tokens))
            ),
            "stream": runtime.exchange.stream,
        }
        temperature = parameters.get("temperature", model_snapshot.get("temperature", self.config.temperature))
        if temperature is not None:
            payload["temperature"] = temperature
        thinking = parameters.get("thinking")
        if isinstance(thinking, Mapping) and thinking.get("type") == "enabled":
            payload["thinking"] = {"type": "enabled"}
        if system:
            payload["system"] = system
        if runtime.exchange.allowed_tools:
            payload["tools"] = [
                {"name": spec.name, "description": spec.description, "input_schema": spec.parameters}
                for spec in runtime.exchange.allowed_tools
            ]
        if isinstance(required_tool_name, str) and required_tool_name:
            payload["tool_choice"] = {"type": "tool", "name": required_tool_name}
        runtime.exchange.request = payload
        return payload

    def prepare_response(self, runtime: AgentRuntime) -> PreparedResponse:
        raw = runtime.exchange.raw_response
        try:
            parsed = self._parse_stream(runtime, raw) if not isinstance(raw, Mapping) else self._parse_json(raw)
        except ModelRequestError as exc:
            raise ProviderOutputError(str(exc), operation=runtime.exchange.operation) from exc
        runtime.exchange.prepared_response = parsed
        runtime.state.turn_usage = parsed.usage
        return parsed

    def _parse_json(self, data: Mapping[str, Any]) -> PreparedResponse:
        blocks = data.get("content")
        if not isinstance(blocks, list):
            raise ModelRequestError("Messages response content must be an array.")
        text: list[str] = []
        reasoning: list[str] = []
        tools: list[ToolMessage] = []
        for block in blocks:
            if not isinstance(block, Mapping):
                continue
            kind = block.get("type")
            if kind == "text":
                text.append(str(block.get("text") or ""))
            elif kind in {"thinking", "redacted_thinking"}:
                reasoning.append(str(block.get("thinking") or block.get("text") or ""))
            elif kind == "tool_use":
                tools.append(
                    ToolMessage(
                        name=str(block.get("name") or ""),
                        call_id=str(block.get("id") or ""),
                        arguments=dict(block.get("input") or {}),
                        status="pending",
                    )
                )
        usage = data.get("usage")
        usage = dict(usage) if isinstance(usage, Mapping) else None
        return PreparedResponse(
            AssistantMessage(
                content="".join(text) or None,
                reasoning="".join(reasoning) or None,
                tool_messages=tools,
                provider_options={"messages": {"response": copy.deepcopy(dict(data))}},
            ),
            usage=usage,
            response_id=str(data.get("id")) if data.get("id") else None,
            model=str(data.get("model")) if data.get("model") else None,
            finish_reason=str(data.get("stop_reason")) if data.get("stop_reason") else None,
            provider_metadata={"type": "message"},
        )

    def _parse_stream(self, runtime: AgentRuntime, events: Iterable[dict[str, Any]]) -> PreparedResponse:
        text: list[str] = []
        reasoning: list[str] = []
        calls: dict[str, dict[str, Any]] = {}
        usage: dict[str, Any] | None = None
        stop_reason: str | None = None
        for event in events:
            kind = str(event.get("__sse_event") or event.get("type") or "")
            if kind == "content_block_start":
                block = event.get("content_block")
                if isinstance(block, Mapping) and block.get("type") == "tool_use":
                    key = str(block.get("id") or len(calls))
                    calls[key] = {"id": key, "name": str(block.get("name") or ""), "input": ""}
            elif kind == "content_block_delta":
                delta = event.get("delta")
                if isinstance(delta, Mapping):
                    if isinstance(delta.get("text"), str):
                        text.append(delta["text"])
                        if runtime.exchange.on_content:
                            runtime.exchange.on_content(delta["text"])
                    if isinstance(delta.get("thinking"), str):
                        reasoning.append(delta["thinking"])
                        if runtime.exchange.on_reasoning:
                            runtime.exchange.on_reasoning(delta["thinking"])
                    if isinstance(delta.get("partial_json"), str):
                        index = int(event.get("index", 0))
                        keys = list(calls)
                        if index < len(keys):
                            calls[keys[index]]["input"] += delta["partial_json"]
            elif kind == "message_delta":
                if isinstance(event.get("delta"), Mapping):
                    stop_reason = str(event["delta"].get("stop_reason") or "") or stop_reason
                if isinstance(event.get("usage"), Mapping):
                    usage = dict(event["usage"])
            elif kind == "message_start" and isinstance(event.get("message"), Mapping):
                if isinstance(event["message"].get("usage"), Mapping):
                    usage = dict(event["message"]["usage"])
        tools = []
        for value in calls.values():
            try:
                arguments = json.loads(value["input"] or "{}")
            except ValueError as exc:
                raise ModelRequestError("Messages streamed tool arguments are invalid JSON.") from exc
            tools.append(ToolMessage(name=value["name"], call_id=value["id"], arguments=arguments, status="pending"))
        return PreparedResponse(
            AssistantMessage(content="".join(text) or None, reasoning="".join(reasoning) or None, tool_messages=tools),
            usage=usage,
            finish_reason=stop_reason,
        )
