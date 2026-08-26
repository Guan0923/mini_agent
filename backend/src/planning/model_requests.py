"""Execute provider-neutral model requests with runtime hooks and events."""

from __future__ import annotations

import copy
from collections.abc import Mapping
from dataclasses import replace
from typing import Protocol

from backend.domain import ToolSpec
from backend.runtime.core.context import AgentRuntime, PreparedResponse
from backend.runtime.core.events import RuntimeEvent
from backend.runtime.core.hooks import (
    HookErrorInfo,
    HookOutcome,
    HookRejected,
    ModelHookContext,
    ModelHookResult,
    RunHookInfo,
    after_model_hook_manager,
    before_model_hook_manager,
)
from backend.runtime.persistence.recording import model_error_data, model_request_data, model_response_data


class RuntimeCompletionClient(Protocol):
    def run(self, runtime: AgentRuntime) -> PreparedResponse: ...


class ModelRequestExecutor:
    """Own the mutable exchange, hooks, and observability for one model call."""

    def __init__(self, client: RuntimeCompletionClient) -> None:
        self._client = client

    def run(
        self,
        runtime: AgentRuntime,
        messages: list,
        *,
        operation: str,
        output_mode: str,
        allowed_tools: list[ToolSpec] | None = None,
        operation_tools: list[ToolSpec] | None = None,
        stream: bool | None = None,
    ) -> PreparedResponse:
        runtime.apply_pending_runtime_config()
        if runtime.state.current_run is not None and runtime.state.running_mode in {"agent", "plan"}:
            runtime.state.current_run.mode = runtime.state.running_mode  # type: ignore[assignment]
        prepare_runtime = getattr(self._client, "prepare_runtime", None)
        if callable(prepare_runtime):
            prepare_runtime(runtime)
        exchange = runtime.exchange
        # Capture dynamic provider/model/permission/mode values once.  A UI
        # PATCH may arrive while transport is in flight, but it must only be
        # consumed at the next model/tool decision boundary.
        # This is the request boundary.  Everything below, including adapter
        # selection and payload construction, must use this detached copy.
        # A PATCH may update the dynamic node while transport is in flight,
        # but that change belongs to the next boundary.
        exchange.context["runtime_config_snapshot"] = copy.deepcopy(
            {
                "provider_name": runtime.state.provider_name,
                "model": runtime.state.model,
                "model_snapshot": runtime.state.model_snapshot,
                "permission_mode": runtime.state.permission_mode,
                "running_mode": runtime.state.running_mode,
                "request_parameters": runtime.state.request_parameters,
            }
        )
        exchange.operation = operation  # type: ignore[assignment]
        exchange.output_mode = output_mode  # type: ignore[assignment]
        exchange.messages = messages
        exchange.allowed_tools = list(allowed_tools or [])
        exchange.operation_tools = list(operation_tools if operation_tools is not None else allowed_tools or [])
        exchange.stream = (
            exchange.on_reasoning is not None or exchange.on_content is not None if stream is None else stream
        )
        exchange.exchange_id = runtime.next_exchange_id()

        config = runtime.request_config()
        parameters = dict(config.get("request_parameters") or {})
        snapshot = config.get("model_snapshot", {})
        if isinstance(snapshot, dict):
            if snapshot.get("output_length") is not None:
                parameters["max_tokens"] = snapshot.get("output_length")
            if snapshot.get("temperature") is not None:
                parameters["temperature"] = snapshot.get("temperature")
            if snapshot.get("thinking") != "disable":
                parameters["thinking"] = {"type": "enabled"}
                if snapshot.get("reasoning_effort") is not None:
                    parameters["reasoning_effort"] = snapshot.get("reasoning_effort")
            else:
                parameters.pop("reasoning_effort", None)
                parameters["thinking"] = {"type": "disabled"}
        overrides = exchange.context.get("request_parameters")
        if isinstance(overrides, dict):
            parameters.update(overrides)
        required_tool = parameters.get("required_tool_name")
        exchange.required_tool_name = required_tool if isinstance(required_tool, str) and required_tool else None
        context = ModelHookContext(
            run=RunHookInfo(
                runtime.state.session_id,
                runtime.run.run_id,
                runtime.run.task,
                runtime.run.mode,
            ),
            operation=operation,
            exchange_id=exchange.exchange_id,
            output_mode=output_mode,
            stream=exchange.stream,
            messages=exchange.messages,
            allowed_tools=exchange.operation_tools,
            request_parameters=parameters,
        )

        previous_parameters = exchange.context.get("request_parameters")
        had_parameters = "request_parameters" in exchange.context
        exchange.context["request_parameters"] = parameters
        try:
            publish = runtime.services.publish or (lambda _event: None)
            before = before_model_hook_manager.execute(context, publish)
            if before.decision == "reject":
                raise HookRejected("model", before.reason or "Model request rejected by hook.", before.data)
            try:
                result = self._send(runtime)
            except Exception as error:
                after_model_hook_manager.execute(
                    replace(
                        context,
                        outcome=HookOutcome(status="failed", error=HookErrorInfo.from_exception(error)),
                    ),
                    publish,
                )
                raise
            after_model_hook_manager.execute(
                replace(context, outcome=self._hook_outcome(result)),
                publish,
            )
            if exchange.required_tool_name:
                runtime.state.request_parameters.pop("required_tool_name", None)
            return result
        finally:
            if had_parameters:
                exchange.context["request_parameters"] = previous_parameters
            else:
                exchange.context.pop("request_parameters", None)

    def _send(
        self,
        runtime: AgentRuntime,
    ) -> PreparedResponse:
        exchange = runtime.exchange
        raw_parameters = exchange.context.get("request_parameters")
        parameters = (
            dict(raw_parameters) if isinstance(raw_parameters, Mapping) else dict(runtime.state.request_parameters)
        )
        estimate_input = getattr(self._client, "estimate_input_tokens", None)
        if callable(estimate_input):
            exchange.context["estimated_input_tokens"] = estimate_input(
                exchange.messages,
                exchange.allowed_tools,
                parameters,
            )

        if getattr(self._client, "records_runtime_events", False):
            return self._client.run(runtime)

        publish = runtime.services.publish or (lambda _event: None)
        publish(
            RuntimeEvent(
                "model_request",
                f"Model {exchange.operation} request",
                model_request_data(runtime.state, exchange),
            )
        )
        try:
            prepared = self._client.run(runtime)
        except Exception as exc:
            publish(
                RuntimeEvent(
                    "model_error",
                    f"Model {exchange.operation} failed",
                    model_error_data(runtime.state, exchange, exc),
                )
            )
            raise
        publish(
            RuntimeEvent(
                "model_response",
                f"Model {exchange.operation} response",
                model_response_data(runtime.state, exchange, prepared),
            )
        )
        return prepared

    @staticmethod
    def _hook_outcome(prepared: PreparedResponse) -> HookOutcome:
        return HookOutcome(
            status="succeeded",
            result=ModelHookResult(
                copy.deepcopy(prepared.message),
                copy.deepcopy(prepared.usage),
                prepared.response_id,
                prepared.model,
                prepared.finish_reason,
            ),
        )
