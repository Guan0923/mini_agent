"""DeepSeek-specific request construction and Chat Completions parsing."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from .client import ChatCompletionsClient
from .config import ModelConfig
from .errors import ModelRequestError


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


@dataclass(frozen=True)
class DeepSeekCompletion:
    id: str
    model: str
    content: str | None
    reasoning_content: str | None
    finish_reason: str | None
    tool_calls: list[DeepSeekToolCall]
    usage: DeepSeekUsage | None


@dataclass(frozen=True)
class DeepSeekStreamDelta:
    content: str | None
    reasoning_content: str | None
    finish_reason: str | None = None


class DeepSeekChatCompletions:
    """DeepSeek adapter for non-streaming `/chat/completions` requests.

    It explicitly enables JSON mode because Mini-Agent's planner consumes a
    JSON plan. DeepSeek requires the prompt to request JSON as well; that
    responsibility stays with the planner.
    """

    def __init__(self, config: ModelConfig, client: ChatCompletionsClient | None = None) -> None:
        self.config = config
        self.client = client or ChatCompletionsClient()

    def complete(self, messages: list[dict[str, str]]) -> str:
        content, _ = self.complete_with_reasoning(messages)
        return content

    def complete_with_reasoning(self, messages: list[dict[str, str]]) -> tuple[str, str | None]:
        """Return the visible content together with DeepSeek thinking, if present."""
        completion = self.create(messages)
        if not completion.content or not completion.content.strip():
            raise ModelRequestError("DeepSeek response did not contain text content.")
        reasoning = completion.reasoning_content.strip() if completion.reasoning_content else None
        return completion.content.strip(), reasoning

    def create(
        self,
        messages: list[dict[str, str]],
        *,
        max_tokens: int | None = None,
        temperature: float = 0,
    ) -> DeepSeekCompletion:
        data = self.client.post_json(
            endpoint=self.config.endpoint,
            api_key=self.config.api_key,
            payload=self._request_payload(messages, max_tokens=max_tokens or self.config.max_tokens, temperature=temperature),
            timeout_seconds=self.config.timeout_seconds,
        )
        return self._parse_response(data)

    def stream_with_reasoning(
        self,
        messages: list[dict[str, str]],
        *,
        max_tokens: int | None = None,
        temperature: float = 0,
    ) -> Iterator[DeepSeekStreamDelta]:
        """Yield DeepSeek reasoning and content chunks from a streaming response."""
        payload = self._request_payload(messages, max_tokens=max_tokens or self.config.max_tokens, temperature=temperature, stream=True)
        for event in self.client.stream_json(
            endpoint=self.config.endpoint,
            api_key=self.config.api_key,
            payload=payload,
            timeout_seconds=self.config.timeout_seconds,
        ):
            try:
                choice = event["choices"][0]
                delta = choice.get("delta", {})
                content = delta.get("content")
                reasoning = delta.get("reasoning_content")
                finish_reason = choice.get("finish_reason")
            except (KeyError, IndexError, TypeError) as exc:
                raise ModelRequestError("DeepSeek stream does not match the Chat Completions schema.") from exc
            if content is not None and not isinstance(content, str):
                raise ModelRequestError("DeepSeek stream content must be text.")
            if reasoning is not None and not isinstance(reasoning, str):
                raise ModelRequestError("DeepSeek stream reasoning must be text.")
            if finish_reason is not None and not isinstance(finish_reason, str):
                raise ModelRequestError("DeepSeek stream finish_reason must be text.")
            yield DeepSeekStreamDelta(content=content, reasoning_content=reasoning, finish_reason=finish_reason)

    def _request_payload(
        self,
        messages: list[dict[str, str]],
        *,
        max_tokens: int,
        temperature: float,
        stream: bool = False,
    ) -> dict[str, Any]:
        return {
            "model": self.config.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
            "stream": stream,
        }

    @staticmethod
    def _parse_response(data: dict[str, Any]) -> DeepSeekCompletion:
        try:
            choice = data["choices"][0]
            message = choice["message"]
            content = message.get("content")
            reasoning_content = message.get("reasoning_content")
            tool_calls = [
                DeepSeekToolCall(
                    id=call["id"],
                    name=call["function"]["name"],
                    arguments=call["function"]["arguments"],
                )
                for call in message.get("tool_calls") or []
            ]
            usage_data = data.get("usage")
            usage = (
                DeepSeekUsage(
                    prompt_tokens=usage_data.get("prompt_tokens"),
                    completion_tokens=usage_data.get("completion_tokens"),
                    total_tokens=usage_data.get("total_tokens"),
                    reasoning_tokens=(usage_data.get("completion_tokens_details") or {}).get("reasoning_tokens"),
                )
                if isinstance(usage_data, dict)
                else None
            )
            if content is not None and not isinstance(content, str):
                raise TypeError("content")
            if reasoning_content is not None and not isinstance(reasoning_content, str):
                raise TypeError("reasoning_content")
            return DeepSeekCompletion(
                id=data["id"],
                model=data["model"],
                content=content,
                reasoning_content=reasoning_content,
                finish_reason=choice.get("finish_reason"),
                tool_calls=tool_calls,
                usage=usage,
            )
        except (KeyError, IndexError, TypeError) as exc:
            raise ModelRequestError("DeepSeek response does not match the Chat Completions schema.") from exc
