"""Deterministic in-memory message queue used as an injectable local test port."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from threading import RLock

from backend.domain.message_queue import (
    ClaimedEnvelope,
    DeliveryConflict,
    MessageEnvelope,
    QueuedMessage,
    QueueItemConflict,
    QueueItemNotFound,
    QueueItemStateConflict,
    queue_utc_now,
)

from .message_queue_support import _envelope_fingerprint, _fingerprint, _merge, _same_create


class MemoryMessageQueue:
    """Injectable deterministic test port; real Redis coverage lives in integration tests."""

    def __init__(self) -> None:
        self._queues: dict[str, list[QueuedMessage]] = {}
        self._streams: dict[str, list[tuple[str, MessageEnvelope, float, str | None]]] = {}
        self._thread_streams: dict[str, list[tuple[str, MessageEnvelope, float, str | None]]] = {}
        self._report_streams: dict[str, list[tuple[str, MessageEnvelope, float, str | None]]] = {}
        self._receipts: dict[str, tuple[str, str, MessageEnvelope]] = {}
        self._counter = 0
        self._lock = RLock()

    def ping(self) -> None:
        return None

    def close(self) -> None:
        return None

    def list(self, thread_id: str) -> list[QueuedMessage]:
        with self._lock:
            return list(self._queues.get(thread_id, ()))

    def create(self, item: QueuedMessage) -> tuple[QueuedMessage, bool]:
        with self._lock:
            items = self._queues.setdefault(item.thread_id, [])
            existing = next((value for value in items if value.id == item.id), None)
            if existing is not None:
                if _same_create(existing, item):
                    return existing, False
                raise QueueItemConflict("queued_message_id_conflict")
            items.append(item)
            return item, True

    def update(
        self, thread_id: str, message_id: str, *, content: str, references: Sequence[dict[str, str]]
    ) -> QueuedMessage:
        with self._lock:
            items = self._queues.get(thread_id, [])
            for index, item in enumerate(items):
                if item.id != message_id:
                    continue
                if item.state != "pending":
                    raise QueueItemStateConflict("queued_message_dispatched")
                items[index] = replace(item, content=content, references=tuple(references), updated_at=queue_utc_now())
                return items[index]
        raise QueueItemNotFound("queued_message_not_found")

    def delete(self, thread_id: str, message_id: str) -> None:
        with self._lock:
            items = self._queues.get(thread_id, [])
            for index, item in enumerate(items):
                if item.id == message_id:
                    if item.state != "pending":
                        raise QueueItemStateConflict("queued_message_dispatched")
                    items.pop(index)
                    return
        raise QueueItemNotFound("queued_message_not_found")

    def dispatch(
        self,
        *,
        delivery_id: str,
        message_ids: Sequence[str],
        session_id: str,
        thread_id: str,
        turn_id: str,
        correlation_id: str | None = None,
    ) -> MessageEnvelope:
        with self._lock:
            fingerprint = _fingerprint(thread_id, turn_id, message_ids)
            receipt = self._receipts.get(delivery_id)
            if receipt is not None:
                if receipt[0] != fingerprint:
                    raise DeliveryConflict("delivery_id_conflict")
                if receipt[1] != "returned":
                    return receipt[2]
            by_id = {item.id: item for item in self._queues.get(thread_id, [])}
            try:
                selected = [by_id[item.id] for item in self._queues.get(thread_id, []) if item.id in set(message_ids)]
            except KeyError as exc:
                raise QueueItemNotFound("queued_message_not_found") from exc
            if len(selected) != len(message_ids):
                raise QueueItemNotFound("queued_message_not_found")
            if any(item.state != "pending" for item in selected):
                raise QueueItemStateConflict("queued_message_dispatched")
            content, references = _merge(selected)
            envelope = (
                receipt[2]
                if receipt is not None
                else MessageEnvelope(
                    delivery_id,
                    "user",
                    thread_id,
                    "turn",
                    turn_id,
                    session_id,
                    thread_id,
                    {"content": content, "references": list(references)},
                    tuple(item.id for item in selected),
                    correlation_id=correlation_id,
                )
            )
            selected_ids = set(envelope.source_message_ids)
            self._queues[thread_id] = [
                replace(item, state="dispatched", updated_at=queue_utc_now()) if item.id in selected_ids else item
                for item in self._queues[thread_id]
            ]
            self._counter += 1
            self._streams.setdefault(turn_id, []).append((f"{self._counter}-0", envelope, 0.0, None))
            self._receipts[delivery_id] = (fingerprint, "dispatched", envelope)
            return envelope

    def dispatch_agent(self, envelope: MessageEnvelope) -> MessageEnvelope:
        if envelope.sender_kind != "agent" or envelope.target_kind != "thread":
            raise ValueError("Agent dispatch requires sender_kind=agent and target_kind=thread.")
        with self._lock:
            fingerprint = _envelope_fingerprint(envelope)
            receipt = self._receipts.get(envelope.delivery_id)
            if receipt is not None:
                if receipt[0] != fingerprint:
                    raise DeliveryConflict("delivery_id_conflict")
                return receipt[2]
            canonical = replace(envelope, attempts=0)
            self._counter += 1
            self._thread_streams.setdefault(envelope.target_id, []).append((f"{self._counter}-0", canonical, 0.0, None))
            self._receipts[envelope.delivery_id] = (fingerprint, "dispatched", canonical)
            return canonical

    def dispatch_report(self, envelope: MessageEnvelope) -> MessageEnvelope:
        if envelope.sender_kind != "agent" or envelope.target_kind != "report":
            raise ValueError("Report dispatch requires sender_kind=agent and target_kind=report.")
        with self._lock:
            fingerprint = _envelope_fingerprint(envelope)
            receipt = self._receipts.get(envelope.delivery_id)
            if receipt is not None:
                if receipt[0] != fingerprint:
                    raise DeliveryConflict("delivery_id_conflict")
                return receipt[2]
            canonical = replace(envelope, attempts=0)
            self._counter += 1
            self._report_streams.setdefault(envelope.target_id, []).append((f"{self._counter}-0", canonical, 0.0, None))
            self._receipts[envelope.delivery_id] = (fingerprint, "dispatched", canonical)
            return canonical

    def claim(self, turn_id: str, consumer: str) -> ClaimedEnvelope | None:
        with self._lock:
            entries = self._streams.get(turn_id, [])
            for index, (stream_id, envelope, claimed_at, owner) in enumerate(entries):
                if owner is None:
                    claimed = replace(envelope, attempts=envelope.attempts + 1)
                    entries[index] = (stream_id, claimed, claimed_at + 1, consumer)
                    return ClaimedEnvelope(stream_id, claimed)
            return None

    def claim_thread(self, thread_id: str, consumer: str) -> ClaimedEnvelope | None:
        with self._lock:
            entries = self._thread_streams.get(thread_id, [])
            for index, (stream_id, envelope, claimed_at, owner) in enumerate(entries):
                if owner is None:
                    claimed = replace(envelope, attempts=envelope.attempts + 1)
                    entries[index] = (stream_id, claimed, claimed_at + 1, consumer)
                    return ClaimedEnvelope(stream_id, claimed)
            return None

    def claim_thread_recovery(self, thread_id: str, consumer: str) -> ClaimedEnvelope | None:
        with self._lock:
            entries = self._thread_streams.get(thread_id, [])
            for index, (stream_id, envelope, claimed_at, _owner) in enumerate(entries):
                claimed = replace(envelope, attempts=envelope.attempts + 1)
                entries[index] = (stream_id, claimed, claimed_at + 1, consumer)
                return ClaimedEnvelope(stream_id, claimed)
            return None

    def claim_report(self, thread_id: str, consumer: str, *, recover: bool = False) -> ClaimedEnvelope | None:
        with self._lock:
            entries = self._report_streams.get(thread_id, [])
            for index, (stream_id, envelope, claimed_at, owner) in enumerate(entries):
                if owner is None or recover:
                    claimed = replace(envelope, attempts=envelope.attempts + 1)
                    entries[index] = (stream_id, claimed, claimed_at + 1, consumer)
                    return ClaimedEnvelope(stream_id, claimed)
            return None

    def peek_thread(self, thread_id: str) -> MessageEnvelope | None:
        with self._lock:
            entries = self._thread_streams.get(thread_id, [])
            return entries[0][1] if entries else None

    def ack(self, claimed: ClaimedEnvelope) -> None:
        with self._lock:
            envelope = claimed.envelope
            if envelope.target_kind in {"thread", "report"}:
                streams = self._thread_streams if envelope.target_kind == "thread" else self._report_streams
                streams[envelope.target_id] = [
                    item for item in streams.get(envelope.target_id, []) if item[0] != claimed.stream_id
                ]
                fingerprint, _, stored = self._receipts[envelope.delivery_id]
                self._receipts[envelope.delivery_id] = (fingerprint, "acknowledged", stored)
                return
            self._streams[envelope.target_id] = [
                item for item in self._streams.get(envelope.target_id, []) if item[0] != claimed.stream_id
            ]
            ids = set(envelope.source_message_ids)
            self._queues[envelope.thread_id] = [
                item for item in self._queues.get(envelope.thread_id, []) if item.id not in ids
            ]
            fingerprint, _, stored = self._receipts[envelope.delivery_id]
            self._receipts[envelope.delivery_id] = (fingerprint, "acknowledged", stored)

    def release_turn(self, turn_id: str) -> None:
        with self._lock:
            entries = self._streams.pop(turn_id, [])
            for _, envelope, _, _ in entries:
                ids = set(envelope.source_message_ids)
                self._queues[envelope.thread_id] = [
                    replace(item, state="pending", updated_at=queue_utc_now()) if item.id in ids else item
                    for item in self._queues.get(envelope.thread_id, [])
                ]
                fingerprint, _, stored = self._receipts[envelope.delivery_id]
                self._receipts[envelope.delivery_id] = (fingerprint, "returned", stored)

    def pending_deliveries(self) -> list[ClaimedEnvelope]:
        with self._lock:
            return [
                ClaimedEnvelope(stream_id, envelope)
                for entries in (*self._streams.values(), *self._thread_streams.values(), *self._report_streams.values())
                for stream_id, envelope, _, _ in entries
            ]


__all__ = ["MemoryMessageQueue"]
