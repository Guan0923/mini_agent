"""DeepSeek adapter entry point and tokenizer integration."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any

from backend.domain import AssistantMessage, ChatMessage, ToolSpec
from backend.runtime.core.context import AgentRuntime, PreparedResponse

from ..config import ModelConfig
from ..errors import ModelConfigurationError, ModelRequestError, ModelTransportError, ProviderOutputError
from .messages import _tool_definition, _wire_messages_from
from .requests import _prepare_request
from .responses import _parse_response, _parse_stream


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
        """Return the legacy request budget including the configured output ceiling."""

        return self.estimate_input_tokens(messages, tools, request_parameters) + self._max_tokens(request_parameters)

    def estimate_input_tokens(
        self,
        messages: list[ChatMessage],
        tools: list[ToolSpec],
        request_parameters: dict[str, Any],
    ) -> int:
        """Estimate request input without treating the output ceiling as consumed context."""

        payload: dict[str, Any] = {"messages": _wire_messages_from(messages)}
        if tools:
            payload["tools"] = [_tool_definition(spec) for spec in tools]
        serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        return len(self._get_tokenizer().encode(serialized).ids)

    def estimate_output_tokens(self, message: AssistantMessage) -> int:
        """Estimate streamed output until the provider returns final usage."""

        payload = {
            "content": message.content or "",
            "reasoning": message.reasoning or "",
            "tool_calls": [{"name": tool.name, "arguments": tool.arguments} for tool in message.tool_messages],
        }
        serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        return len(self._get_tokenizer().encode(serialized).ids)

    def _max_tokens(self, request_parameters: dict[str, Any]) -> int:
        max_tokens = request_parameters.get("max_tokens", self.config.max_tokens)
        if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens < 1:
            max_tokens = self.config.max_tokens
        return max_tokens

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
