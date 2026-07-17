"""Provider selection plus generic JSON HTTP transport."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any, Protocol
from urllib.parse import urlsplit, urlunsplit

import requests

from mini_agent.domain import ChatMessage, ToolSpec
from mini_agent.runtime.context import AgentRuntime, PreparedResponse
from mini_agent.runtime.events import RuntimeEvent
from mini_agent.runtime.recording import model_error_data, model_request_data, model_response_data

from .config import ModelConfig
from .deepseek import DeepSeek
from .errors import ModelConfigurationError, ModelRequestError


class ProviderAdapter(Protocol):
    """Translate between the runtime exchange and one provider wire format."""

    @property
    def endpoint(self) -> str: ...

    @property
    def headers(self) -> dict[str, str]: ...

    @property
    def timeout_seconds(self) -> int: ...

    @property
    def operation(self) -> str: ...

    @property
    def context_size(self) -> int: ...

    def estimate_tokens(
        self,
        messages: list[ChatMessage],
        tools: list[ToolSpec],
        request_parameters: dict[str, Any],
    ) -> int: ...

    def prepare_request(self, runtime: AgentRuntime) -> dict[str, Any]: ...

    def prepare_response(self, runtime: AgentRuntime) -> PreparedResponse: ...


class JsonHttpTransport:
    """Perform schema-neutral JSON POSTs and consume SSE JSON events."""

    def __init__(self, session: requests.Session | None = None) -> None:
        self.session = session or requests.Session()

    def post_json(
        self,
        endpoint: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        timeout_seconds: int,
    ) -> dict[str, Any]:
        try:
            response = self.session.post(
                endpoint,
                headers=headers,
                json=payload,
                timeout=timeout_seconds,
                allow_redirects=False,
            )
            response.raise_for_status()
            data = response.json()
        except requests.RequestException as exc:
            raise ModelRequestError(f"Model request failed: {exc.__class__.__name__}") from exc
        except ValueError as exc:
            raise ModelRequestError("Model response is not valid JSON.") from exc
        if not isinstance(data, dict):
            raise ModelRequestError("Model response must be a JSON object.")
        return data

    def stream_json(
        self,
        endpoint: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        timeout_seconds: int,
    ) -> Iterator[dict[str, Any]]:
        response: requests.Response | None = None
        saw_done = False
        try:
            response = self.session.post(
                endpoint,
                headers=headers,
                json=payload,
                timeout=timeout_seconds,
                stream=True,
                allow_redirects=False,
            )
            response.raise_for_status()
            for line in response.iter_lines(decode_unicode=True):
                if not line:
                    continue
                if isinstance(line, bytes):
                    try:
                        line = line.decode("utf-8")
                    except UnicodeDecodeError as exc:
                        raise ModelRequestError("Stream event is not valid UTF-8.") from exc
                if not line.startswith("data:"):
                    continue
                raw_event = line.removeprefix("data:").strip()
                if raw_event == "[DONE]":
                    saw_done = True
                    return
                try:
                    event = json.loads(raw_event)
                except json.JSONDecodeError as exc:
                    raise ModelRequestError("Stream event is not valid JSON.") from exc
                if not isinstance(event, dict):
                    raise ModelRequestError("Stream event must be a JSON object.")
                yield event
            if not saw_done:
                raise ModelRequestError("Model stream ended before [DONE].")
        except requests.RequestException as exc:
            raise ModelRequestError(f"Model stream failed: {exc.__class__.__name__}") from exc
        finally:
            if response is not None:
                response.close()


class LLMClient:
    """Coordinate one provider adapter with the shared HTTP transport."""

    records_runtime_events = True

    def __init__(
        self,
        config: ModelConfig,
        session: requests.Session | None = None,
        transport: JsonHttpTransport | None = None,
        adapter: ProviderAdapter | None = None,
    ) -> None:
        if session is not None and transport is not None:
            raise ValueError("Provide either session or transport, not both.")
        self.config = config
        self.llm = adapter or self._create_llm(config)
        self.transport = transport or JsonHttpTransport(session)
        self._last_request_diagnostics: dict[str, Any] = {}

    @property
    def context_size(self) -> int:
        return self.config.context_size

    def estimate_tokens(
        self,
        messages: list[ChatMessage],
        tools: list[ToolSpec],
        request_parameters: dict[str, Any],
    ) -> int:
        estimate = getattr(self.llm, "estimate_tokens", None)
        if not callable(estimate):
            raise ModelConfigurationError(
                f"Provider {self.config.provider!r} does not support local context token estimation."
            )
        return estimate(messages, tools, request_parameters)

    def run(self, runtime: AgentRuntime) -> PreparedResponse:
        self._last_request_diagnostics = {}
        runtime.state.provider = self.config.provider
        runtime.state.model = self.config.model
        runtime.state.request_parameters.setdefault("max_tokens", self.config.max_tokens)
        if runtime.exchange.exchange_id is None:
            runtime.exchange.exchange_id = runtime.next_exchange_id()
        publish = runtime.services.publish or (lambda _event: None)
        publish(
            RuntimeEvent(
                "model_request",
                f"Model {runtime.exchange.operation or 'completion'} request",
                model_request_data(runtime.state, runtime.exchange),
            )
        )
        diagnostics = self._request_diagnostics(runtime.exchange.stream)
        raw: dict[str, Any] | Iterator[dict[str, Any]] | None = None
        completed = False
        try:
            payload = self.llm.prepare_request(runtime)
            if runtime.exchange.stream:
                raw = self.transport.stream_json(
                    self.llm.endpoint,
                    self.llm.headers,
                    payload,
                    self.llm.timeout_seconds,
                )
            else:
                raw = self.transport.post_json(
                    self.llm.endpoint,
                    self.llm.headers,
                    payload,
                    self.llm.timeout_seconds,
                )
            runtime.exchange.raw_response = raw
            prepared = self.llm.prepare_response(runtime)
            completed = True
        except ModelRequestError as exc:
            diagnostics.update(request_outcome="failed", request_error_message=str(exc))
            self._last_request_diagnostics = diagnostics
            failure = ModelRequestError(str(exc), diagnostics=diagnostics)
            publish(
                RuntimeEvent(
                    "model_error",
                    f"Model {runtime.exchange.operation or 'completion'} failed",
                    model_error_data(runtime.state, runtime.exchange, failure),
                )
            )
            raise failure from exc
        except Exception as exc:
            publish(
                RuntimeEvent(
                    "model_error",
                    f"Model {runtime.exchange.operation or 'completion'} failed",
                    model_error_data(runtime.state, runtime.exchange, exc),
                )
            )
            raise
        finally:
            if not completed and runtime.exchange.stream and raw is not None:
                close = getattr(raw, "close", None)
                if callable(close):
                    close()
                if runtime.exchange.raw_response is raw:
                    runtime.exchange.raw_response = None
        diagnostics.update(
            request_outcome="completed",
            response_id=prepared.response_id,
            response_model=prepared.model,
            finish_reason=prepared.finish_reason,
            content_chars=len(prepared.message.content or ""),
            reasoning_chars=len(prepared.message.reasoning or ""),
            usage=prepared.usage,
        )
        self._last_request_diagnostics = diagnostics
        publish(
            RuntimeEvent(
                "model_response",
                f"Model {runtime.exchange.operation or 'completion'} response",
                model_response_data(runtime.state, runtime.exchange, prepared),
            )
        )
        return prepared

    def consume_request_diagnostics(self) -> dict[str, Any]:
        diagnostics = self._last_request_diagnostics
        self._last_request_diagnostics = {}
        return diagnostics

    @staticmethod
    def _create_llm(config: ModelConfig) -> ProviderAdapter:
        if config.provider == "deepseek":
            return DeepSeek(config)
        raise ModelConfigurationError(f"Unsupported model provider: {config.provider!r}.")

    def _request_diagnostics(self, stream: bool) -> dict[str, Any]:
        return {
            "provider": self.config.provider,
            "operation": self.llm.operation,
            "model": self.config.model,
            "endpoint": self._safe_endpoint(self.llm.endpoint),
            "stream": stream,
        }

    @staticmethod
    def _safe_endpoint(endpoint: str) -> str:
        parsed = urlsplit(endpoint)
        host = parsed.netloc.rsplit("@", maxsplit=1)[-1]
        return urlunsplit((parsed.scheme, host, parsed.path, "", ""))
