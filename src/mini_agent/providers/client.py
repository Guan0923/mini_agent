"""Provider selection plus generic JSON HTTP transport."""

from __future__ import annotations

import copy
import json
import time
from collections.abc import Iterator
from time import perf_counter
from typing import Any, Protocol
from urllib.parse import urlsplit, urlunsplit

import requests

from mini_agent.domain import ChatMessage, ModelOutputError, ToolSpec
from mini_agent.runtime.core.context import AgentRuntime, PreparedResponse
from mini_agent.runtime.core.events import RuntimeEvent
from mini_agent.runtime.persistence.recording import model_error_data, model_request_data, model_response_data

from .config import ModelConfig
from .deepseek import DeepSeek
from .errors import ModelConfigurationError, ModelRequestError, ModelTransportError


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
        self.last_metadata: dict[str, Any] = {}

    def post_json(
        self,
        endpoint: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        timeout_seconds: int,
    ) -> dict[str, Any]:
        started = perf_counter()
        response: requests.Response | None = None
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
            raise _transport_error(exc, "Model request failed") from exc
        except ValueError as exc:
            raise ModelTransportError("Model response is not valid JSON.", retryable=True) from exc
        finally:
            self.last_metadata = {
                "http_status": getattr(response, "status_code", None),
                "response_headers": _safe_response_headers(getattr(response, "headers", None)),
                "transport_duration_ms": round((perf_counter() - started) * 1000, 3),
            }
        if not isinstance(data, dict):
            raise ModelTransportError("Model response must be a JSON object.", retryable=True)
        return data

    def stream_json(
        self,
        endpoint: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        timeout_seconds: int,
    ) -> Iterator[dict[str, Any]]:
        started = perf_counter()
        response: requests.Response | None = None
        saw_done = False
        saw_event = False
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
                        raise ModelTransportError(
                            "Stream event is not valid UTF-8.", retryable=not saw_event, stream_started=saw_event
                        ) from exc
                if not line.startswith("data:"):
                    continue
                raw_event = line.removeprefix("data:").strip()
                if raw_event == "[DONE]":
                    saw_done = True
                    return
                try:
                    event = json.loads(raw_event)
                except json.JSONDecodeError as exc:
                    raise ModelTransportError(
                        "Stream event is not valid JSON.", retryable=not saw_event, stream_started=saw_event
                    ) from exc
                if not isinstance(event, dict):
                    raise ModelTransportError(
                        "Stream event must be a JSON object.", retryable=not saw_event, stream_started=saw_event
                    )
                saw_event = True
                yield event
            if not saw_done:
                raise ModelTransportError(
                    "Model stream ended before [DONE].", retryable=not saw_event, stream_started=saw_event
                )
        except requests.RequestException as exc:
            raise _transport_error(exc, "Model stream failed", stream_started=saw_event) from exc
        finally:
            if response is not None:
                response.close()
            self.last_metadata = {
                "http_status": getattr(response, "status_code", None),
                "response_headers": _safe_response_headers(getattr(response, "headers", None)),
                "transport_duration_ms": round((perf_counter() - started) * 1000, 3),
                "stream_completed": saw_done,
            }


def _safe_response_headers(headers: Any) -> dict[str, str]:
    if not hasattr(headers, "items"):
        return {}
    allowed = {"content-type", "request-id", "x-request-id", "retry-after"}
    return {
        str(key).lower(): str(value)
        for key, value in headers.items()
        if str(key).lower() in allowed
    }


_RETRYABLE_STATUS_CODES = frozenset({408, 429, 500, 502, 503, 504})


def _transport_error(
    error: requests.RequestException,
    label: str,
    *,
    stream_started: bool = False,
) -> ModelTransportError:
    response = getattr(error, "response", None)
    status_code = getattr(response, "status_code", None)
    retry_after: float | None = None
    headers = getattr(response, "headers", None)
    if headers is not None:
        raw_retry_after = headers.get("Retry-After")
        try:
            retry_after = min(30.0, max(0.0, float(raw_retry_after)))
        except (TypeError, ValueError):
            retry_after = None
    retryable = isinstance(error, (requests.Timeout, requests.ConnectionError)) or status_code in _RETRYABLE_STATUS_CODES
    return ModelTransportError(
        f"{label}: {error.__class__.__name__}",
        retryable=retryable and not stream_started,
        status_code=status_code if isinstance(status_code, int) else None,
        retry_after=retry_after,
        stream_started=stream_started,
    )


class _RecordedStream:
    """Capture every parsed stream event while preserving incremental iteration."""

    def __init__(self, source: Iterator[dict[str, Any]]) -> None:
        self._source = source
        self.events: list[dict[str, Any]] = []
        self.completed = False

    def __iter__(self) -> _RecordedStream:
        return self

    def __next__(self) -> dict[str, Any]:
        try:
            event = next(self._source)
        except StopIteration:
            self.completed = True
            raise
        self.events.append(copy.deepcopy(event))
        return event

    def close(self) -> None:
        close = getattr(self._source, "close", None)
        if callable(close):
            close()


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
            prepared = self.llm.prepare_response(runtime)
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
            runtime.exchange.transport_metadata["response_body_bytes"] = len(
                json.dumps(runtime.exchange.wire_response, ensure_ascii=False, default=str).encode("utf-8")
            ) if runtime.exchange.wire_response is not None else 0
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
