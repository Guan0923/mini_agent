"""Consume user steering at explicit runtime safe points."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from backend.domain import UserMessage

from ..core.context import AgentRuntime
from ..core.events import RuntimeEvent


@dataclass(frozen=True)
class SteeringUpdate:
    content: str
    message_count: int
    delivery_id: str = ""
    references: tuple[dict[str, str], ...] = ()
    ack: Callable[[], None] | None = None


def collect_steering(runtime: AgentRuntime) -> SteeringUpdate | None:
    """Atomically drain the process-local steering handler without blocking."""

    handler = runtime.services.steering
    if handler is None:
        return None
    raw_messages = handler()
    messages: list[str] = []
    delivery_id = ""
    ack: Callable[[], None] | None = None
    references: list[dict[str, str]] = []
    seen_references: set[tuple[str, str]] = set()
    for raw in raw_messages:
        if isinstance(raw, Mapping):
            content = str(raw.get("content") or "").strip()
            delivery_id = delivery_id or str(raw.get("delivery_id") or "")
            candidate_ack = raw.get("_ack")
            if ack is None and callable(candidate_ack):
                ack = candidate_ack
            for value in raw.get("references", []):
                if not isinstance(value, Mapping):
                    continue
                source, path = str(value.get("source") or ""), str(value.get("path") or "")
                key = (source, path)
                if source in {"project", "upload"} and path and key not in seen_references:
                    seen_references.add(key)
                    references.append({"source": source, "path": path})
        else:
            content = str(raw).strip()
        if content or references:
            messages.append(content)
    if not messages:
        return None
    return SteeringUpdate("\n\n".join(messages), len(messages), delivery_id, tuple(references), ack)


def apply_steering(runtime: AgentRuntime, update: SteeringUpdate, *, phase: str) -> None:
    """Append one merged user message and persist it before execution continues."""

    publish = runtime.services.publish or (lambda _event: None)
    data = {
        "message_count": update.message_count,
        "phase": phase,
        "delivery_id": update.delivery_id,
        "references": list(update.references),
    }
    publish(RuntimeEvent("steering_received", "In-run user input received", data))

    already_applied = bool(update.delivery_id) and any(
        event.kind == "steering_applied" and event.data.get("delivery_id") == update.delivery_id
        for event in runtime.run.events
    )
    if not already_applied:
        runtime.state.messages.append(UserMessage(content=update.content))
        runtime.run.history = runtime.state.messages
    store = runtime.services.runtime_store
    if store is not None:
        store.append_turn_input(
            runtime.state.session_id,
            runtime.run.run_id,
            update.content,
            delivery_id=update.delivery_id or None,
        )
    if not already_applied:
        runtime.run.add_event("steering_applied", "In-run user input applied", **data)
    # The content travels with the event so the message-tree bridge can
    # persist the steering input as a first-class user node; without it the
    # canonical node projection would silently drop the message.
    publish(RuntimeEvent("steering_applied", "In-run user input applied", {**data, "content": update.content}))
    runtime.save()
    if update.ack is not None:
        update.ack()


def consume_steering(runtime: AgentRuntime, *, phase: str) -> SteeringUpdate | None:
    update = collect_steering(runtime)
    if update is not None:
        apply_steering(runtime, update, phase=phase)
    return update
