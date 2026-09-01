"""Shared execution workflow helpers."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from backend.domain import AssistantMessage, ToolMessage, message_to_dict
from backend.planning import PlannerCapabilities

from ...conversation.steering import SteeringUpdate, apply_steering
from ...core.context import AgentRuntime
from ...core.events import RuntimeEvent
from ..steps import ToolStepExecutor, ToolStepResult

_MAX_TOOL_CONTEXT_CHARS = 2_000


@dataclass(frozen=True)
class PlanProposalResult:
    """One completed Plan-mode response, optionally submitted for review."""

    message: AssistantMessage
    plan: str | None = None
    content_streamed: bool = False


def _publish(runtime: AgentRuntime, event: RuntimeEvent) -> None:
    (runtime.services.publish or (lambda _event: None))(event)


@dataclass(frozen=True)
class _TextStreamResult:
    reasoning: bool
    content: bool


def _model_text_stream(
    runtime: AgentRuntime,
    *,
    stream_content: bool = False,
) -> Callable[[], _TextStreamResult]:
    reasoning_open = False
    response_open = False
    reasoning_streamed = False
    content_streamed = False

    def close_reasoning() -> None:
        nonlocal reasoning_open
        if reasoning_open:
            _publish(runtime, RuntimeEvent("thinking_end"))
            reasoning_open = False

    def close_response() -> None:
        nonlocal response_open
        if response_open:
            _publish(runtime, RuntimeEvent("response_end"))
            response_open = False

    def on_reasoning(chunk: str) -> None:
        nonlocal reasoning_open, reasoning_streamed
        if not chunk:
            return
        close_response()
        if not reasoning_open:
            _publish(runtime, RuntimeEvent("thinking_start"))
            reasoning_open = True
            reasoning_streamed = True
        _publish(runtime, RuntimeEvent("thinking_delta", chunk))

    def on_content(chunk: str) -> None:
        nonlocal response_open, content_streamed
        if not chunk:
            return
        close_reasoning()
        if not response_open:
            _publish(runtime, RuntimeEvent("response_start"))
            response_open = True
            content_streamed = True
        _publish(runtime, RuntimeEvent("response_delta", chunk))

    runtime.exchange.on_reasoning = on_reasoning
    runtime.exchange.on_content = on_content if stream_content else None

    def close() -> _TextStreamResult:
        runtime.exchange.on_reasoning = None
        runtime.exchange.on_content = None
        close_reasoning()
        close_response()
        return _TextStreamResult(reasoning_streamed, content_streamed)

    return close


def _publish_repairs(runtime: AgentRuntime, capabilities: PlannerCapabilities) -> None:
    reporter = capabilities.output_repair_reporter
    if reporter is None:
        return
    for repair in reporter.consume_output_repairs():
        if not isinstance(repair, dict):
            continue
        outcome = repair.get("outcome")
        message = (
            "Malformed model output was repaired automatically."
            if outcome == "repaired"
            else "Malformed model output could not be repaired automatically."
        )
        # Keep repair diagnostics in the internal runtime/audit stream. The
        # event publisher filters this kind from user-facing SSE and nodes,
        # while the correction message remains in the next model request.
        _publish(runtime, RuntimeEvent("model_repair", message, dict(repair)))


def _publish_assistant_message(
    runtime: AgentRuntime,
    message: AssistantMessage,
    streamed: _TextStreamResult,
) -> None:
    """Publish one transient boundary after a completed assistant response."""

    data = {
        "message": message_to_dict(message),
        "exchange_id": runtime.exchange.exchange_id,
        "reasoning_streamed": streamed.reasoning,
        "content_streamed": streamed.content,
    }
    normalized_usage = runtime.exchange.context.get("node_usage")
    if isinstance(normalized_usage, dict):
        data["usage"] = dict(normalized_usage)
    _publish(
        runtime,
        RuntimeEvent(
            "assistant_message",
            data=data,
        ),
    )


def _publish_tool_call(runtime: AgentRuntime, tool: ToolMessage) -> None:
    _publish(
        runtime,
        RuntimeEvent("tool_call", tool.name, {"call_id": tool.call_id, "arguments": dict(tool.arguments)}),
    )


def _publish_tool_result(runtime: AgentRuntime, tool: ToolMessage) -> None:
    result = tool.content or ""
    _publish(runtime, RuntimeEvent("tool_result", result, {"tool": tool.name, "call_id": tool.call_id}))


def _publish_tool_failure(runtime: AgentRuntime, tool: ToolMessage, error: str) -> None:
    _publish(runtime, RuntimeEvent("tool_failed", error, {"tool": tool.name, "call_id": tool.call_id}))


def _publish_tool_recovery(runtime: AgentRuntime, tool: ToolMessage, error: str) -> None:
    """Record recoverable tool feedback without exposing it to the user stream."""

    attempt = len(runtime.run.actions)
    _publish(
        runtime,
        RuntimeEvent(
            "tool_recovery",
            _truncate(error),
            {"tool": tool.name, "call_id": tool.call_id, "attempt": attempt},
        ),
    )


def _fail_pending_tools(
    runtime: AgentRuntime,
    message: AssistantMessage,
    error: str,
    *,
    failure_code: str | None = None,
) -> None:
    """Close unexecuted tool calls after cancellation or steering."""

    for tool in message.tool_messages:
        if tool.status == "pending":
            tool.status = "failed"
            tool.content = error
            tool.retryable = False
            tool.failure_code = failure_code
            if failure_code is None:
                _publish_tool_failure(runtime, tool, error)
                continue
            _publish(
                runtime,
                RuntimeEvent(
                    "tool_failed",
                    error,
                    {
                        "tool": tool.name,
                        "call_id": tool.call_id,
                        "error": error,
                        "failure_code": failure_code,
                    },
                ),
            )


def _truncate(value: str) -> str:
    if len(value) <= _MAX_TOOL_CONTEXT_CHARS:
        return value
    omitted = len(value) - _MAX_TOOL_CONTEXT_CHARS
    return f"{value[:_MAX_TOOL_CONTEXT_CHARS]}… ({omitted} characters omitted)"


def _tool_failure_content(tool: ToolMessage, error: str) -> str:
    del tool
    return _truncate(error)


def _start_assistant(runtime: AgentRuntime, message: AssistantMessage) -> None:
    runtime.state.active_message = message
    runtime.state.active_tool_index = None
    runtime.save()


def _finish_assistant(runtime: AgentRuntime) -> None:
    message = runtime.state.active_message
    if message is None:
        return
    runtime.state.messages.append(message)
    runtime.run.history = runtime.state.messages
    runtime.state.active_message = None
    runtime.state.active_tool_index = None
    runtime.save()


def _execute_tool(runtime: AgentRuntime, index: int, executor: ToolStepExecutor) -> ToolStepResult:
    message = runtime.state.active_message
    assert message is not None
    tool = message.tool_messages[index]
    runtime.state.active_tool_index = index
    runtime.run.actions.append(tool)
    return executor.execute(runtime)


def _consume_agent_reports(runtime: AgentRuntime) -> int:
    consume = getattr(runtime.services.subagents, "consume_runtime_reports", None)
    return int(consume(runtime) or 0) if callable(consume) else 0


def _apply_tool_batch_reports(runtime: AgentRuntime, *, next_tool_index: int) -> None:
    """Stop work selected without newly arrived Assistant reports in context."""

    message = runtime.state.active_message
    if message is not None:
        _fail_pending_tools(runtime, message, "Not executed because a subagent report arrived.")
        _finish_assistant(runtime)
    else:
        runtime.state.active_tool_index = None
    del next_tool_index


def _apply_tool_batch_steering(
    runtime: AgentRuntime,
    update: SteeringUpdate,
    *,
    next_tool_index: int,
    phase: str,
) -> None:
    """Preserve completed calls and close any unexecuted calls before steering."""

    message = runtime.state.active_message
    if message is not None and next_tool_index > 0:
        _fail_pending_tools(runtime, message, "Not executed because the user supplied new instructions.")
        _finish_assistant(runtime)
    else:
        runtime.state.active_message = None
        runtime.state.active_tool_index = None
    apply_steering(runtime, update, phase=phase)
