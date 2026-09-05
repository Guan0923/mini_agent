"""Durable queued-message and mailbox delivery contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from .file_paths import FILE_SOURCES, is_reference_path

QueueMessageState = Literal["pending", "dispatched"]
SenderKind = Literal["user", "agent", "system"]
TargetKind = Literal["turn", "turn_start", "thread", "report"]


def queue_utc_now() -> str:
    return datetime.now(UTC).isoformat()


class MessageQueueError(RuntimeError):
    """Base class for stable queue failures."""


class MessageQueueUnavailable(MessageQueueError):
    """Redis is unavailable, so queue-dependent work must fail closed."""


class QueueItemNotFound(MessageQueueError):
    """A queued message does not exist in the requested Thread."""


class QueueItemConflict(MessageQueueError):
    """A queued message ID was reused with different immutable input."""


class QueueItemStateConflict(MessageQueueError):
    """The requested mutation is invalid for the queued message state."""


class DeliveryConflict(MessageQueueError):
    """A delivery ID was reused for a different dispatch."""


@dataclass(frozen=True, slots=True)
class QueuedMessage:
    id: str
    thread_id: str
    content: str
    references: tuple[dict[str, str], ...] = ()
    state: QueueMessageState = "pending"
    created_at: str = field(default_factory=queue_utc_now)
    updated_at: str = field(default_factory=queue_utc_now)

    def __post_init__(self) -> None:
        if not self.id or not self.thread_id:
            raise ValueError("QueuedMessage id and thread_id are required.")
        if self.state not in {"pending", "dispatched"}:
            raise ValueError("QueuedMessage state is invalid.")
        if not self.content.strip() and not self.references:
            raise ValueError("QueuedMessage requires content or references.")
        if any(
            reference.get("source") not in FILE_SOURCES
            or not reference.get("path")
            or not reference.get("display_path")
            or not is_reference_path(reference.get("path"))
            for reference in self.references
        ):
            raise ValueError("QueuedMessage contains an invalid reference.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "thread_id": self.thread_id,
            "content": self.content,
            "references": [dict(value) for value in self.references],
            "state": self.state,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> QueuedMessage:
        return cls(
            id=str(value.get("id") or ""),
            thread_id=str(value.get("thread_id") or ""),
            content=str(value.get("content") or ""),
            references=tuple(dict(item) for item in value.get("references", []) if isinstance(item, dict)),
            state=str(value.get("state") or ""),  # type: ignore[arg-type]
            created_at=str(value.get("created_at") or queue_utc_now()),
            updated_at=str(value.get("updated_at") or queue_utc_now()),
        )


@dataclass(frozen=True, slots=True)
class MessageEnvelope:
    delivery_id: str
    sender_kind: SenderKind
    source_thread_id: str
    target_kind: TargetKind
    target_id: str
    session_id: str
    thread_id: str
    payload: dict[str, Any]
    source_message_ids: tuple[str, ...]
    created_at: str = field(default_factory=queue_utc_now)
    correlation_id: str | None = None
    attempts: int = 0

    def __post_init__(self) -> None:
        required = (
            self.delivery_id,
            self.source_thread_id,
            self.target_id,
            self.session_id,
            self.thread_id,
        )
        if not all(required):
            raise ValueError("MessageEnvelope identifiers are required.")
        if self.sender_kind not in {"user", "agent", "system"}:
            raise ValueError("MessageEnvelope sender_kind is invalid.")
        if self.target_kind not in {"turn", "turn_start", "thread", "report"}:
            raise ValueError("MessageEnvelope target_kind is invalid.")
        if not self.source_message_ids or len(set(self.source_message_ids)) != len(self.source_message_ids):
            raise ValueError("MessageEnvelope source_message_ids must be non-empty and unique.")
        if isinstance(self.attempts, bool) or self.attempts < 0:
            raise ValueError("MessageEnvelope attempts must be non-negative.")
        if not self.content.strip() and not self.references:
            raise ValueError("MessageEnvelope payload requires content or references.")
        if self.sender_kind == "agent":
            invalid_references = any(
                not (
                    (
                        set(reference) == {"path"}
                        and bool(reference.get("path"))
                        and is_reference_path(reference.get("path"))
                    )
                    or (
                        set(reference) == {"source", "path", "display_path"}
                        and reference.get("source") in FILE_SOURCES
                        and bool(reference.get("path"))
                        and bool(reference.get("display_path"))
                        and is_reference_path(reference.get("path"))
                    )
                )
                for reference in self.references
            )
        else:
            invalid_references = any(
                reference.get("source") not in FILE_SOURCES
                or not reference.get("path")
                or not reference.get("display_path")
                or not is_reference_path(reference.get("path"))
                for reference in self.references
            )
        if invalid_references:
            raise ValueError("MessageEnvelope contains an invalid reference.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "delivery_id": self.delivery_id,
            "sender_kind": self.sender_kind,
            "source_thread_id": self.source_thread_id,
            "target_kind": self.target_kind,
            "target_id": self.target_id,
            "session_id": self.session_id,
            "thread_id": self.thread_id,
            "payload": dict(self.payload),
            "source_message_ids": list(self.source_message_ids),
            "created_at": self.created_at,
            "correlation_id": self.correlation_id,
            "attempts": self.attempts,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> MessageEnvelope:
        return cls(
            delivery_id=str(value.get("delivery_id") or ""),
            sender_kind=str(value.get("sender_kind") or ""),  # type: ignore[arg-type]
            source_thread_id=str(value.get("source_thread_id") or ""),
            target_kind=str(value.get("target_kind") or ""),  # type: ignore[arg-type]
            target_id=str(value.get("target_id") or ""),
            session_id=str(value.get("session_id") or ""),
            thread_id=str(value.get("thread_id") or ""),
            payload=dict(value.get("payload") or {}),
            source_message_ids=tuple(str(item) for item in value.get("source_message_ids", [])),
            created_at=str(value.get("created_at") or queue_utc_now()),
            correlation_id=str(value["correlation_id"]) if value.get("correlation_id") else None,
            attempts=int(value.get("attempts") or 0),
        )

    @property
    def content(self) -> str:
        return str(self.payload.get("content") or "")

    @property
    def references(self) -> tuple[dict[str, str], ...]:
        return tuple(dict(item) for item in self.payload.get("references", []) if isinstance(item, dict))


@dataclass(frozen=True, slots=True)
class ClaimedEnvelope:
    stream_id: str
    envelope: MessageEnvelope


__all__ = [
    "ClaimedEnvelope",
    "DeliveryConflict",
    "MessageEnvelope",
    "MessageQueueError",
    "MessageQueueUnavailable",
    "QueueItemConflict",
    "QueueItemNotFound",
    "QueueItemStateConflict",
    "QueuedMessage",
    "queue_utc_now",
]
