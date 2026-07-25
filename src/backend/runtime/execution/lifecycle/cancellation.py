"""Cooperative run cancellation at explicit runtime safe points."""

from __future__ import annotations

from ...core.context import AgentRuntime
from ...core.events import RuntimeEvent
from .outcomes import cancel_run, suspend_run


def cancel_if_requested(runtime: AgentRuntime) -> bool:
    """Cancel the active run once when its process-local signal is set."""

    suspend = runtime.services.suspend_requested
    if suspend is not None and suspend():
        if runtime.run.status == "suspended":
            return True
        if runtime.run.status == "running":
            suspend_run(runtime)
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
                runtime.run.add_event(
                    "tool_failed",
                    f"{tool.name} failed",
                    call_id=tool.call_id,
                    error=tool.content,
                )
                publish = runtime.services.publish or (lambda _event: None)
                publish(
                    RuntimeEvent(
                        "tool_failed",
                        tool.content,
                        {"tool": tool.name, "call_id": tool.call_id},
                    )
                )
        if not any(message is active for message in runtime.state.messages):
            runtime.state.messages.append(active)
        runtime.run.history = runtime.state.messages
        runtime.state.active_message = None
        runtime.state.active_tool_index = None
    cancel_run(runtime)
    return True
