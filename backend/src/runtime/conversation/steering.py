"""Consume user steering at explicit runtime safe points."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from backend.domain import UserMessage

from ..core.context import AgentRuntime
from ..core.events import RuntimeEvent


@dataclass(frozen=True)
class SteeringUpdate:
    content: str
    message_count: int
    steering_id: str = ""
    references: tuple[dict[str, str], ...] = ()


def collect_steering(runtime: AgentRuntime) -> SteeringUpdate | None:
    """Atomically drain the process-local steering handler without blocking."""

    handler = runtime.services.steering
    if handler is None:
        return None
    raw_messages = handler()
    messages: list[str] = []
    steering_id = ""
    references: list[dict[str, str]] = []
    seen_references: set[tuple[str, str]] = set()
    for raw in raw_messages:
        if isinstance(raw, Mapping):
            content = str(raw.get("content") or "").strip()
            steering_id = steering_id or str(raw.get("steering_id") or "")
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
    return SteeringUpdate("\n\n".join(messages), len(messages), steering_id, tuple(references))


def apply_steering(runtime: AgentRuntime, update: SteeringUpdate, *, phase: str) -> None:
    """Append one merged user message and persist it before execution continues."""

    publish = runtime.services.publish or (lambda _event: None)
    data = {
        "message_count": update.message_count,
        "phase": phase,
        "steering_id": update.steering_id,
        "references": list(update.references),
    }
    publish(RuntimeEvent("steering_received", "In-run user input received", data))

    runtime.state.messages.append(UserMessage(content=update.content))
    runtime.run.history = runtime.state.messages
    store = runtime.services.runtime_store
    if store is not None:
        store.append_turn_input(runtime.state.session_id, runtime.run.run_id, update.content)
    # The content travels with the event so the message-tree bridge can
    # persist the steering input as a first-class user node; without it the
    # canonical node projection would silently drop the message.
    publish(RuntimeEvent("steering_applied", "In-run user input applied", {**data, "content": update.content}))
    runtime.save()


def consume_steering(runtime: AgentRuntime, *, phase: str) -> SteeringUpdate | None:
    update = collect_steering(runtime)
    if update is not None:
        apply_steering(runtime, update, phase=phase)
    return update
