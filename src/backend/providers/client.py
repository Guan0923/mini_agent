"""Provider selection plus generic JSON HTTP transport."""

from __future__ import annotations

import copy
import json
import time
from collections.abc import Iterator, Mapping
from time import perf_counter
from typing import Any, Protocol
from urllib.parse import urlsplit, urlunsplit

import requests

from backend.domain import AssistantMessage, ChatMessage, ModelOutputError, ToolSpec
from backend.runtime.core.context import AgentRuntime, PreparedResponse
from backend.runtime.core.events import RuntimeEvent
from backend.runtime.persistence.recording import model_error_data, model_request_data, model_response_data

from .config import ModelConfig
from .deepseek import DeepSeek
from .errors import ModelConfigurationError, ModelRequestError, ModelTransportError
from .transport import JsonHttpTransport, _RecordedStream


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

    def estimate_input_tokens(
        self,
        messages: list[ChatMessage],
        tools: list[ToolSpec],
        request_parameters: dict[str, Any],
    ) -> int:
        estimate = getattr(self.llm, "estimate_input_tokens", None)
        if callable(estimate):
            return estimate(messages, tools, request_parameters)
        return self.estimate_tokens(messages, tools, request_parameters)

    def run(self, runtime: AgentRuntime) -> PreparedResponse:
        runtime.state.provider = self.config.provider
        runtime.state.model = self.config.model
        runtime.state.request_parameters.setdefault("max_tokens", self.config.max_tokens)
        if runtime.exchange.exchange_id is None:
            runtime.exchange.exchange_id = runtime.next_exchange_id()
        publish = runtime.services.publish or (lambda _event: None)
        max_retries = runtime.state.runner_settings.max_transport_retries
        for attempt in range(max_retries + 1):
            try:
                prepared = self._run_once(runtime, attempt=attempt + 1)
                self._last_request_diagnostics["transport_attempts"] = attempt + 1
                return prepared
            except ModelOutputError as exc:
                self._publish_request_failure(runtime, exc, publish)
                raise
            except ModelTransportError as exc:
                if exc.retryable and attempt < max_retries:
                    delay = exc.retry_after if exc.retry_after is not None else 0.5 * (2**attempt)
                    publish(
                        RuntimeEvent(
                            "model_retry",
                            str(exc),
                            {
                                "attempt": attempt + 1,
                                "max_retries": max_retries,
                                "delay_seconds": delay,
                                "status_code": exc.status_code,
                            },
                        )
                    )
                    time.sleep(delay)
                    continue
                self._publish_request_failure(runtime, exc, publish)
                raise
            except ModelRequestError as exc:
                self._publish_request_failure(runtime, exc, publish)
                raise
        raise AssertionError("Transport retry loop ended without an outcome.")

    @staticmethod
    def _publish_request_failure(runtime: AgentRuntime, error: ModelRequestError, publish) -> None:
        publish(
            RuntimeEvent(
                "model_error",
                f"Model {runtime.exchange.operation or 'completion'} failed",
                model_error_data(runtime.state, runtime.exchange, error),
            )
        )

    def _run_once(self, runtime: AgentRuntime, *, attempt: int) -> PreparedResponse:
        self._last_request_diagnostics = {}
        runtime.state.provider = self.config.provider
        runtime.state.model = self.config.model
        runtime.state.request_parameters.setdefault("max_tokens", self.config.max_tokens)
        if runtime.exchange.exchange_id is None:
            runtime.exchange.exchange_id = runtime.next_exchange_id()
        publish = runtime.services.publish or (lambda _event: None)
        self._begin_token_usage(runtime)
        diagnostics = self._request_diagnostics(runtime.exchange.stream)
        started = perf_counter()
        raw: dict[str, Any] | Iterator[dict[str, Any]] | None = None
        recorded_stream: _RecordedStream | None = None
        completed = False
        try:
            payload = self.llm.prepare_request(runtime)
            runtime.exchange.wire_request = copy.deepcopy(payload)
            runtime.exchange.transport_metadata = {
                **diagnostics,
                "http_method": "POST",
                "attempt": attempt,
                "request_body_bytes": len(json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")),
            }
            publish(
                RuntimeEvent(
                    "model_request",
                    f"Model {runtime.exchange.operation or 'completion'} request",
                    model_request_data(runtime.state, runtime.exchange),
                )
            )
            if runtime.exchange.stream:
                source = self.transport.stream_json(
                    self.llm.endpoint,
                    self.llm.headers,
                    payload,
                    self.llm.timeout_seconds,
                )
                recorded_stream = _RecordedStream(source)
                raw = recorded_stream
            else:
                raw = self.transport.post_json(
                    self.llm.endpoint,
                    self.llm.headers,
                    payload,
                    self.llm.timeout_seconds,
                )
            runtime.exchange.raw_response = raw
            previous_reasoning = runtime.exchange.on_reasoning
            previous_content = runtime.exchange.on_content
            self._track_stream_output(runtime, previous_reasoning, previous_content)
            try:
                prepared = self.llm.prepare_response(runtime)
            finally:
                runtime.exchange.on_reasoning = previous_reasoning
                runtime.exchange.on_content = previous_content
            self._complete_token_usage(runtime, prepared.usage, prepared.message)
            completed = True
        except ModelRequestError as exc:
            diagnostics.update(request_outcome="failed", request_error_message=str(exc))
            exc.diagnostics = {**diagnostics, **exc.diagnostics}
            self._last_request_diagnostics = exc.diagnostics
            raise
        except ModelOutputError as exc:
            diagnostics.update(request_outcome="failed", request_error_message=str(exc))
            exc.diagnostics = {**diagnostics, **getattr(exc, "diagnostics", {})}
            self._last_request_diagnostics = exc.diagnostics
            raise
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
            runtime.exchange.transport_metadata.update(getattr(self.transport, "last_metadata", {}) or {})
            runtime.exchange.transport_metadata["duration_ms"] = round((perf_counter() - started) * 1000, 3)
            if recorded_stream is not None:
                runtime.exchange.wire_response = list(recorded_stream.events)
                runtime.exchange.transport_metadata["stream_completed"] = recorded_stream.completed
            elif isinstance(raw, dict):
                runtime.exchange.wire_response = copy.deepcopy(raw)
            runtime.exchange.transport_metadata["response_body_bytes"] = (
                len(json.dumps(runtime.exchange.wire_response, ensure_ascii=False, default=str).encode("utf-8"))
                if runtime.exchange.wire_response is not None
                else 0
            )
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

    def _begin_token_usage(self, runtime: AgentRuntime) -> None:
        exchange_id = runtime.exchange.exchange_id
        if not exchange_id:
            return
        estimated_input = runtime.exchange.context.get("estimated_input_tokens")
        if isinstance(estimated_input, bool) or not isinstance(estimated_input, int) or estimated_input < 0:
            return
        requests = self._token_requests(runtime)
        entry = requests.setdefault(exchange_id, {})
        entry.update(
            {
                "exchange_id": exchange_id,
                "estimated_input_tokens": estimated_input,
                "estimated_output_tokens": 0,
                "provider_prompt_tokens": None,
                "provider_completion_tokens": None,
                "provider_total_tokens": None,
            }
        )
        self._refresh_token_usage(runtime)
        self._publish_context_usage(runtime, entry, phase="estimated")

    def _track_stream_output(self, runtime: AgentRuntime, previous_reasoning, previous_content) -> None:
        if not runtime.exchange.stream:
            return
        reasoning_parts: list[str] = []
        content_parts: list[str] = []

        def refresh() -> None:
            exchange_id = runtime.exchange.exchange_id
            if not exchange_id:
                return
            estimate = getattr(self.llm, "estimate_output_tokens", None)
            if not callable(estimate):
                return
            message = AssistantMessage(
                content="".join(content_parts) or None,
                reasoning="".join(reasoning_parts) or None,
            )
            entry = self._token_requests(runtime).get(exchange_id)
            if entry is None:
                return
            entry["estimated_output_tokens"] = estimate(message)
            self._refresh_token_usage(runtime)

        def on_reasoning(chunk: str) -> None:
            reasoning_parts.append(chunk)
            refresh()
            if previous_reasoning is not None:
                previous_reasoning(chunk)

        def on_content(chunk: str) -> None:
            content_parts.append(chunk)
            refresh()
            if previous_content is not None:
                previous_content(chunk)

        runtime.exchange.on_reasoning = on_reasoning
        runtime.exchange.on_content = on_content

    def _complete_token_usage(
        self, runtime: AgentRuntime, usage: dict[str, Any] | None, message: AssistantMessage
    ) -> None:
        exchange_id = runtime.exchange.exchange_id
        if not exchange_id:
            return
        entry = self._token_requests(runtime).get(exchange_id)
        if entry is None:
            return
        estimate_output = getattr(self.llm, "estimate_output_tokens", None)
        if callable(estimate_output):
            entry["estimated_output_tokens"] = estimate_output(message)
        if isinstance(usage, Mapping):
            entry["provider_prompt_tokens"] = self._token_value(usage.get("prompt_tokens"))
            entry["provider_completion_tokens"] = self._token_value(usage.get("completion_tokens"))
            entry["provider_total_tokens"] = self._token_value(usage.get("total_tokens"))
        self._refresh_token_usage(runtime)
        self._publish_context_usage(runtime, entry, phase="provider")

    @staticmethod
    def _token_value(value: Any) -> int | None:
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None

    @staticmethod
    def _token_requests(runtime: AgentRuntime) -> dict[str, dict[str, Any]]:
        requests = runtime.state.token_usage.setdefault("requests", {})
        if not isinstance(requests, dict):
            requests = {}
            runtime.state.token_usage["requests"] = requests
        return requests

    @staticmethod
    def _request_parameters(runtime: AgentRuntime) -> dict[str, Any]:
        parameters = dict(runtime.state.request_parameters)
        overrides = runtime.exchange.context.get("request_parameters")
        if isinstance(overrides, Mapping):
            parameters.update(overrides)
        return parameters

    @classmethod
    def _refresh_token_usage(cls, runtime: AgentRuntime) -> None:
        requests = cls._token_requests(runtime)
        totals = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
        current_input = 0
        for entry in requests.values():
            estimated_input = cls._token_value(entry.get("estimated_input_tokens")) or 0
            estimated_output = cls._token_value(entry.get("estimated_output_tokens")) or 0
            provider_input = cls._token_value(entry.get("provider_prompt_tokens"))
            provider_output = cls._token_value(entry.get("provider_completion_tokens"))
            provider_total = cls._token_value(entry.get("provider_total_tokens"))
            input_tokens = provider_input if provider_input is not None else estimated_input
            output_tokens = provider_output if provider_output is not None else estimated_output
            if provider_total is not None:
                total_tokens = provider_total
            else:
                total_tokens = input_tokens + output_tokens
            entry.update(
                {
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "total_tokens": total_tokens,
                    "input_source": "provider" if provider_input is not None else "estimated",
                    "output_source": "provider" if provider_output is not None else "estimated",
                    "total_source": "provider" if provider_total is not None else "estimated",
                }
            )
            totals["input_tokens"] += input_tokens
            totals["output_tokens"] += output_tokens
            totals["total_tokens"] += total_tokens
            current_input = input_tokens
        runtime.state.token_usage["totals"] = totals
        runtime.state.token_usage["current_input_tokens"] = current_input

    @classmethod
    def _publish_context_usage(cls, runtime: AgentRuntime, entry: dict[str, Any], *, phase: str) -> None:
        context_size = getattr(runtime.services.planner, "client", None)
        context_size = getattr(context_size, "context_size", None)
        if not isinstance(context_size, int) or context_size < 1:
            return
        input_tokens = int(entry["input_tokens"])
        (runtime.services.publish or (lambda _event: None))(
            RuntimeEvent(
                "context_usage",
                "Context usage reconciled",
                {
                    "estimated_tokens": input_tokens,
                    "input_tokens": input_tokens,
                    "input_source": entry["input_source"],
                    "estimated_input_tokens": entry["estimated_input_tokens"],
                    "estimated_output_tokens": entry["estimated_output_tokens"],
                    "provider_prompt_tokens": entry["provider_prompt_tokens"],
                    "provider_completion_tokens": entry["provider_completion_tokens"],
                    "provider_total_tokens": entry["provider_total_tokens"],
                    "context_size": context_size,
                    "target_ratio": 0.8,
                    "target_tokens": int(context_size * 0.8),
                    "ratio": input_tokens / context_size,
                    "phase": phase,
                    "exchange_id": entry["exchange_id"],
                },
            )
        )

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
