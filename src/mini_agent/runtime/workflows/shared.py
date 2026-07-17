"""Shared state transitions used by runtime workflows."""

from __future__ import annotations

from collections.abc import Callable

from mini_agent.domain import AssistantMessage, ToolMessage

from ..context import AgentRuntime
from ..events import RuntimeEvent
from ..planner import PlannerCapabilities
from ..steering import SteeringUpdate, apply_steering
from ..steps import ToolStepExecutor, ToolStepResult

_MAX_TOOL_CONTEXT_CHARS = 2_000


def _publish(runtime: AgentRuntime, event: RuntimeEvent) -> None:
    (runtime.services.publish or (lambda _event: None))(event)


def _reasoning_stream(runtime: AgentRuntime) -> Callable[[], bool]:
    streamed = False

    def on_reasoning(chunk: str) -> None:
        nonlocal streamed
        if not streamed:
            _publish(runtime, RuntimeEvent("thinking_start"))
            streamed = True
        _publish(runtime, RuntimeEvent("thinking_delta", chunk))

    runtime.exchange.on_reasoning = on_reasoning

    def close() -> bool:
        runtime.exchange.on_reasoning = None
        if streamed:
            _publish(runtime, RuntimeEvent("thinking_end"))
        return streamed

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
        runtime.run.add_event("model_repair", message, **repair)
        _publish(runtime, RuntimeEvent("model_repair", message, repair))


def _record_reasoning(runtime: AgentRuntime, message: AssistantMessage, streamed: bool) -> None:
    if not message.reasoning:
        return
    runtime.run.add_event("reasoning", "Model reasoning", content=message.reasoning)
    if not streamed:
        _publish(runtime, RuntimeEvent("thinking_start"))
        _publish(runtime, RuntimeEvent("thinking_delta", message.reasoning))
        _publish(runtime, RuntimeEvent("thinking_end"))


def _truncate(value: str) -> str:
    if len(value) <= _MAX_TOOL_CONTEXT_CHARS:
        return value
    omitted = len(value) - _MAX_TOOL_CONTEXT_CHARS
    return f"{value[:_MAX_TOOL_CONTEXT_CHARS]}… ({omitted} characters omitted)"


def _same_tool(first: ToolMessage, second: ToolMessage | None) -> bool:
    return second is not None and first.name == second.name and first.arguments == second.arguments


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
    runtime.run.add_event("model", "Model tool call validated", tool=tool.name, mode=runtime.run.mode)
    return executor.execute(runtime)


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
        for tool in message.tool_messages[next_tool_index:]:
            if tool.status == "pending":
                tool.status = "failed"
                tool.content = "Not executed because the user supplied new instructions."
                tool.retryable = False
        _finish_assistant(runtime)
    else:
        runtime.state.active_message = None
        runtime.state.active_tool_index = None
    apply_steering(runtime, update, phase=phase)
