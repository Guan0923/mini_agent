"""Cooperative run cancellation at explicit runtime safe points."""

from __future__ import annotations

from backend.domain import AssistantMessage

from ...core.context import AgentRuntime
from ...core.events import RuntimeEvent
from .outcomes import cancel_run, complete_run, pause_run


def cancel_if_requested(runtime: AgentRuntime) -> bool:
    """Cancel the active run once when its process-local signal is set."""

    if runtime.services.pause_after_tool:
        runtime.services.pause_after_tool = False
        if runtime.run.status != "running":
            return runtime.run.status != "running"
        pause_run(runtime)
        return True

    complete = runtime.services.complete_requested
    if complete is not None and complete():
        if runtime.run.status == "completed":
            return True
        if runtime.run.status == "running":
            active = runtime.state.active_message
            message = active if isinstance(active, AssistantMessage) else AssistantMessage(content="")
            for tool in message.tool_messages:
                if tool.status == "pending":
                    tool.status = "failed"
                    tool.content = "Not executed because the Agent Turn was marked successful."
                    tool.retryable = False
            runtime.state.active_message = None
            runtime.state.active_tool_index = None
            complete_run(runtime, message, final_answer=message.content or "")
            return True

    suspend = runtime.services.suspend_requested
    if suspend is not None and suspend():
        if runtime.run.status == "cancelled" and runtime.run.stop_reason == "user_paused":
            return True
        if runtime.run.status == "running":
            pause_run(runtime)
            return True

    handler = runtime.services.cancel_requested
    if handler is None or not handler():
        return False
    if runtime.run.status == "cancelled":
        return True
    if runtime.run.status != "running":
        return False

    active = runtime.state.active_message
    if active is not None:
        for tool in active.tool_messages:
            if tool.status == "pending":
                tool.status = "failed"
                tool.content = "Not executed because the run was cancelled."
                tool.retryable = False
                publish = runtime.services.publish or (lambda _event: None)
                publish(RuntimeEvent("tool_failed", tool.content, {"tool": tool.name, "call_id": tool.call_id}))
        if not any(message is active for message in runtime.state.messages):
            runtime.state.messages.append(active)
        runtime.run.history = runtime.state.messages
        runtime.state.active_message = None
        runtime.state.active_tool_index = None
    cancel_run(runtime)
    return True
