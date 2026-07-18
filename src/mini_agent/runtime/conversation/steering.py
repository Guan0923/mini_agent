"""Consume user steering at explicit runtime safe points."""

from __future__ import annotations

from dataclasses import dataclass

from mini_agent.domain import UserMessage

from ..core.context import AgentRuntime
from ..core.events import RuntimeEvent


@dataclass(frozen=True)
class SteeringUpdate:
    content: str
    message_count: int


def collect_steering(runtime: AgentRuntime) -> SteeringUpdate | None:
    """Atomically drain the process-local steering handler without blocking."""

    handler = runtime.services.steering
    if handler is None:
        return None
    messages = [message.strip() for message in handler() if message.strip()]
    if not messages:
        return None
    return SteeringUpdate("\n\n".join(messages), len(messages))


def apply_steering(runtime: AgentRuntime, update: SteeringUpdate, *, phase: str) -> None:
    """Append one merged user message and persist it before execution continues."""

    publish = runtime.services.publish or (lambda _event: None)
    data = {"message_count": update.message_count, "phase": phase}
    publish(RuntimeEvent("steering_received", "In-run user input received", data))

    runtime.state.messages.append(UserMessage(content=update.content))
    runtime.run.history = runtime.state.messages
    store = runtime.services.runtime_store
    if store is not None:
        store.append_turn_input(runtime.state.session_id, runtime.run.run_id, update.content)
    runtime.run.add_event("steering_applied", "In-run user input applied", **data)
    publish(RuntimeEvent("steering_applied", "In-run user input applied", data))
    runtime.save()


def consume_steering(runtime: AgentRuntime, *, phase: str) -> SteeringUpdate | None:
    update = collect_steering(runtime)
    if update is not None:
        apply_steering(runtime, update, phase=phase)
    return update
