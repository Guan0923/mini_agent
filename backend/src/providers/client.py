"""Provider selection plus generic JSON HTTP transport."""

from __future__ import annotations

import copy
import json
import time
from collections.abc import Callable, Iterator
from time import perf_counter
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import requests

from backend.domain import AssistantMessage, ChatMessage, ModelOutputError, ToolSpec
from backend.runtime.core.context import AgentRuntime, PreparedResponse
from backend.runtime.core.events import RuntimeEvent
from backend.runtime.persistence.recording import model_error_data, model_request_data, model_response_data

from .adapters import ProviderAdapter
from .config import ModelConfig
from .errors import ModelConfigurationError, ModelRequestError, ModelTransportError
from .protocols import ChatCompletionsAdapter, MessagesAdapter, ResponsesAdapter
from .token_usage import TokenUsageTracker
from .transport import JsonHttpTransport, _RecordedStream


class LLMClient:
    """Coordinate one provider adapter with the shared HTTP transport."""

    records_runtime_events = True

    def __init__(
        self,
        config: ModelConfig,
        session: requests.Session | None = None,
        transport: JsonHttpTransport | None = None,
        adapter: ProviderAdapter | None = None,
        config_resolver: Callable[[str], ModelConfig] | None = None,
    ) -> None:
        if session is not None and transport is not None:
            raise ValueError("Provide either session or transport, not both.")
        self.config = config
        self._adapter_override = adapter is not None
        self._config_resolver = config_resolver
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

    def set_config_resolver(self, resolver: Callable[[str], ModelConfig] | None) -> None:
        """Attach the local provider lookup used by dynamic runs."""

        self._config_resolver = resolver

    def prepare_runtime(self, runtime: AgentRuntime, snapshot: dict[str, Any] | None = None) -> None:
        """Resolve the provider selected at this model-request boundary.

        ``snapshot`` is supplied by :class:`ModelRequestExecutor` after it
        captures the dynamic leaf.  It is important that a later PATCH cannot
        swap the adapter or credentials for a request already in flight.
        """

        captured = snapshot if isinstance(snapshot, dict) else None
        selected = str(
            (captured or {}).get("provider_name") or getattr(runtime.state, "provider_name", "") or ""
        ).strip()
        if self._config_resolver is not None and selected and selected.casefold() != "unknown":
            resolved = self._config_resolver(selected)
            if not isinstance(resolved, ModelConfig):
                raise ModelConfigurationError("Provider resolver returned an invalid model configuration.")
            if not self._adapter_override and resolved != self.config:
                self.config = resolved
                self.llm = self._create_llm(resolved)
        # The runtime starts with an empty state for a new session.  Seed it
        # from the configured provider without overwriting a user selection.
        if not selected or selected.casefold() == "unknown":
            runtime.state.provider_name = self.config.provider_name
            selected = runtime.state.provider_name
        # Fill only missing fields from the selected provider.  The dynamic
        # node is authoritative: a partial runtime-config update must survive
        # the provider lookup and must not be replaced by provider defaults.
        model_snapshot = (
            captured.get("model_snapshot")
            if captured is not None and isinstance(captured.get("model_snapshot"), dict)
            else runtime.state.model_snapshot
        )
        explicit_model = (
            str((captured or {}).get("model") or "")
            if captured is not None
            else (runtime.state.model if runtime.state.model not in {"", "unknown"} else "")
        )
        model_snapshot.setdefault("reasoning_effort", "medium")
        model_snapshot.setdefault("current_model", explicit_model or self.config.model)
        model_snapshot.setdefault("context_length", self.config.context_size)
        model_snapshot.setdefault("output_length", self.config.max_tokens)
        model_snapshot.setdefault("thinking", "enable")
        model_snapshot.setdefault("temperature", self.config.temperature)
        if captured is None:
            runtime.state.model = str(model_snapshot["current_model"])
        elif not runtime.state.model or runtime.state.model == "unknown":
            # Seed an initially empty runtime for callers that invoke the
            # client directly; never overwrite a newer dynamic PATCH.
            runtime.state.model = str(model_snapshot["current_model"])
        # ``provider`` is retained only as the internal adapter kind.  The
        # user-facing/provider-selection identity is always provider_name.
        # ``provider`` is the internal adapter kind used by diagnostics and
        # legacy runtime records.  It must follow the resolved named
        # provider even when this request was captured from a dynamic PATCH.
        runtime.state.provider = self.config.provider

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
        existing_snapshot = runtime.exchange.context.get("runtime_config_snapshot")
        if isinstance(existing_snapshot, dict):
            config_snapshot = copy.deepcopy(existing_snapshot)
            # The executor already consumed pending configuration at the
            # boundary.  Do not consume a concurrent PATCH here.
            self.prepare_runtime(runtime, config_snapshot)
        else:
            runtime.apply_pending_runtime_config()
            self.prepare_runtime(runtime)
            config_snapshot = runtime.request_config()
            runtime.exchange.context["runtime_config_snapshot"] = copy.deepcopy(config_snapshot)
        request_parameters = config_snapshot.get("request_parameters")
        if isinstance(request_parameters, dict):
            request_parameters.setdefault(
                "max_tokens",
                (config_snapshot.get("model_snapshot") or {}).get("output_length", self.config.max_tokens),
            )
            # ``request_config`` returns a detached snapshot.  Persist the
            # completed copy back into the exchange so adapters invoked
            # directly (without ModelRequestExecutor) see the same defaults
            # as the executor path.  This also freezes max_tokens for an
            # already in-flight request when a concurrent PATCH arrives.
            config_snapshot["request_parameters"] = request_parameters
            runtime.exchange.context["runtime_config_snapshot"] = copy.deepcopy(config_snapshot)
        if runtime.exchange.exchange_id is None:
            runtime.exchange.exchange_id = runtime.next_exchange_id()
        publish = runtime.services.publish or (lambda _event: None)
        max_transport_retries = runtime.state.runner_settings.max_transport_retries
        for attempt in range(max_transport_retries + 1):
            try:
                prepared = self._run_once(runtime, attempt=attempt + 1, config_snapshot=config_snapshot)
                self._last_request_diagnostics["transport_attempts"] = attempt + 1
                return prepared
            except ModelOutputError as exc:
                self._publish_request_failure(runtime, exc, publish)
                raise
            except ModelTransportError as exc:
                if exc.retryable and attempt < max_transport_retries:
                    delay = exc.retry_after if exc.retry_after is not None else 0.5 * (2**attempt)
                    publish(
                        RuntimeEvent(
                            "model_retry",
                            str(exc),
                            {
                                "attempt": attempt + 1,
                                "max_transport_retries": max_transport_retries,
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

    def _run_once(
        self,
        runtime: AgentRuntime,
        *,
        attempt: int,
        config_snapshot: dict[str, Any] | None = None,
    ) -> PreparedResponse:
        self._last_request_diagnostics = {}
        self.prepare_runtime(runtime, config_snapshot)
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
                    cancel_requested=runtime.stop_requested,
                    register_abort=runtime.services.register_operation_abort,
                )
                recorded_stream = _RecordedStream(source)
                raw = recorded_stream
            else:
                raw = self.transport.post_json(
                    self.llm.endpoint,
                    self.llm.headers,
                    payload,
                    self.llm.timeout_seconds,
                    cancel_requested=runtime.stop_requested,
                    register_abort=runtime.services.register_operation_abort,
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
            if not completed:
                self._usage_tracker().discard_unconfirmed(runtime)
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

    def _usage_tracker(self) -> TokenUsageTracker:
        return TokenUsageTracker(self.llm)

    def _begin_token_usage(self, runtime: AgentRuntime) -> None:
        self._usage_tracker().begin(runtime)

    def _track_stream_output(self, runtime: AgentRuntime, previous_reasoning, previous_content) -> None:
        self._usage_tracker().track_stream_output(runtime, previous_reasoning, previous_content)

    def _complete_token_usage(
        self, runtime: AgentRuntime, usage: dict[str, Any] | None, message: AssistantMessage
    ) -> None:
        self._usage_tracker().complete(runtime, usage, message)

    def consume_request_diagnostics(self) -> dict[str, Any]:
        diagnostics = self._last_request_diagnostics
        self._last_request_diagnostics = {}
        return diagnostics

    @staticmethod
    def _create_llm(config: ModelConfig) -> ProviderAdapter:
        adapters = {
            "chat_completions": ChatCompletionsAdapter,
            "responses": ResponsesAdapter,
            "messages": MessagesAdapter,
        }
        adapter = adapters.get(config.protocol or "chat_completions")
        if adapter is None:
            raise ModelConfigurationError(f"Unsupported model protocol: {config.protocol!r}.")
        return adapter(config)

    def _request_diagnostics(self, stream: bool) -> dict[str, Any]:
        return {
            "provider": self.config.provider,
            "provider_name": self.config.provider_name,
            "protocol": self.config.protocol,
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
