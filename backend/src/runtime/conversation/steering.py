"""Consume user steering at explicit runtime safe points."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

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
    source_thread_id: str = ""
    need_reply: bool = False


def _model_content_with_references(content: str, references: tuple[dict[str, str], ...]) -> str:
    reference_lines = [
        f"- @{Path(reference['path']).as_posix()}" + (f" ({reference['source']})" if reference.get("source") else "")
        for reference in references
    ]
    if not reference_lines:
        return content
    return f"{content}\n\nFile references:\n" + "\n".join(reference_lines)


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
    source_thread_id = ""
    need_reply = False
    for raw in raw_messages:
        if isinstance(raw, Mapping):
            content = str(raw.get("content") or "").strip()
            delivery_id = delivery_id or str(raw.get("delivery_id") or "")
            candidate_ack = raw.get("_ack")
            if ack is None and callable(candidate_ack):
                ack = candidate_ack
            source_thread_id = source_thread_id or str(raw.get("source_thread_id") or "")
            need_reply = need_reply or bool(raw.get("need_reply", False))
            for value in raw.get("references", []):
                if not isinstance(value, Mapping):
                    continue
                source = str(value.get("source") or "")
                path = str(value.get("path") or "")
                display_path = str(value.get("display_path") or "")
                key = (source, path)
                if (
                    ((source in {"project", "upload"} and display_path) or (not source and Path(path).is_absolute()))
                    and path
                    and key not in seen_references
                ):
                    seen_references.add(key)
                    references.append(
                        {"source": source, "path": path, "display_path": display_path} if source else {"path": path}
                    )
        else:
            content = str(raw).strip()
        if content or references:
            messages.append(content)
    if not messages:
        return None
    return SteeringUpdate(
        "\n\n".join(messages),
        len(messages),
        delivery_id,
        tuple(references),
        ack,
        source_thread_id,
        need_reply,
    )


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

    store = runtime.services.runtime_store
    has_turn_delivery = getattr(store, "has_turn_delivery", None)
    already_applied = (
        bool(update.delivery_id)
        and callable(has_turn_delivery)
        and has_turn_delivery(runtime.state.session_id, update.delivery_id)
    )
    if not already_applied:
        runtime.state.messages.append(
            UserMessage(content=_model_content_with_references(update.content, update.references))
        )
        runtime.run.history = runtime.state.messages
    if store is not None:
        store.append_turn_input(
            runtime.state.session_id,
            runtime.run.run_id,
            update.content,
            delivery_id=update.delivery_id or None,
        )
    # The content travels with the event so the message-tree bridge can
    # persist the steering input as a first-class user node; without it the
    # canonical node projection would silently drop the message.
    publish(RuntimeEvent("steering_applied", "In-run user input applied", {**data, "content": update.content}))
    if update.need_reply:
        register_report = getattr(store, "register_turn_report", None)
        if not callable(register_report) or not update.source_thread_id:
            raise RuntimeError("A reply-requesting Agent message has no canonical report registration path.")
        register_report(runtime.run.turn_id, runtime.run.thread_id, update.source_thread_id)
    runtime.save()
    if update.ack is not None:
        update.ack()


def consume_steering(runtime: AgentRuntime, *, phase: str) -> SteeringUpdate | None:
    update = collect_steering(runtime)
    if update is not None:
        apply_steering(runtime, update, phase=phase)
    return update
