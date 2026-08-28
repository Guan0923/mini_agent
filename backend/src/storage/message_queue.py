"""Redis-backed queued messages and at-least-once Turn mailboxes."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Sequence
from dataclasses import replace
from threading import RLock
from typing import Any

from redis import Redis
from redis.exceptions import RedisError

from backend.domain.message_queue import (
    ClaimedEnvelope,
    DeliveryConflict,
    MessageEnvelope,
    MessageQueueUnavailable,
    QueuedMessage,
    QueueItemConflict,
    QueueItemNotFound,
    QueueItemStateConflict,
    queue_utc_now,
)

DEFAULT_REDIS_URL = "redis://127.0.0.1:6379/0"
DEFAULT_KEY_PREFIX = "mini-agent:v1"
DELIVERY_RECEIPT_TTL_SECONDS = 7 * 24 * 60 * 60
STALE_CLAIM_MS = 60_000
CONSUMER_GROUP = "runtime"


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _fingerprint(thread_id: str, turn_id: str, message_ids: Sequence[str]) -> str:
    raw = _json({"thread_id": thread_id, "turn_id": turn_id, "message_ids": sorted(message_ids)})
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _merge(messages: Sequence[QueuedMessage]) -> tuple[str, tuple[dict[str, str], ...]]:
    references: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for message in messages:
        for reference in message.references:
            key = (str(reference.get("source") or ""), str(reference.get("path") or ""))
            if key not in seen:
                seen.add(key)
                references.append({"source": key[0], "path": key[1]})
    return "\n\n".join(message.content for message in messages), tuple(references)


def _same_create(left: QueuedMessage, right: QueuedMessage) -> bool:
    return (
        left.id == right.id
        and left.thread_id == right.thread_id
        and left.content == right.content
        and left.references == right.references
    )


class RedisMessageQueue:
    """Versioned Redis keyspace for editable drafts and immutable deliveries."""

    _dispatch_script = """
local receipt = KEYS[1]
local messages = KEYS[2]
local stream = KEYS[3]
local existing = redis.call('HGET', receipt, 'fingerprint')
if existing then
  if existing ~= ARGV[1] then return {'delivery_conflict'} end
  if redis.call('HGET', receipt, 'status') ~= 'returned' then
    return {'duplicate', redis.call('HGET', receipt, 'stream_id') or ''}
  end
end
local count = tonumber(ARGV[2])
for index = 1, count do
  local id = ARGV[2 + index]
  local raw = redis.call('HGET', messages, id)
  if not raw then return {'missing', id} end
  local value = cjson.decode(raw)
  if value['state'] ~= 'pending' then return {'state_conflict', id} end
  if raw ~= ARGV[2 + count + index] then return {'queue_changed', id} end
end
for index = 1, count do
  local id = ARGV[2 + index]
  local updated = ARGV[2 + (count * 2) + index]
  redis.call('HSET', messages, id, updated)
end
local envelope = ARGV[3 + (count * 3)]
local stream_id = redis.call('XADD', stream, '*', 'envelope', envelope)
redis.call('HSET', receipt, 'fingerprint', ARGV[1], 'status', 'dispatched', 'stream_id', stream_id, 'attempts', 0, 'envelope', envelope)
redis.call('PERSIST', receipt)
return {'created', stream_id}
"""

    _ack_script = """
local receipt = KEYS[1]
local stream = KEYS[2]
local messages = KEYS[3]
local order = KEYS[4]
local fingerprint = redis.call('HGET', receipt, 'fingerprint')
if not fingerprint then return {'missing_receipt'} end
if fingerprint ~= ARGV[1] then return {'delivery_conflict'} end
local status = redis.call('HGET', receipt, 'status')
if status == 'acknowledged' then return {'duplicate'} end
redis.pcall('XACK', stream, ARGV[2], ARGV[3])
redis.call('XDEL', stream, ARGV[3])
local count = tonumber(ARGV[4])
for index = 1, count do
  local id = ARGV[4 + index]
  redis.call('HDEL', messages, id)
  redis.call('ZREM', order, id)
end
redis.call('HSET', receipt, 'status', 'acknowledged', 'acknowledged_at', ARGV[5 + count])
redis.call('EXPIRE', receipt, tonumber(ARGV[6 + count]))
if redis.call('XLEN', stream) == 0 then redis.call('DEL', stream) end
return {'acknowledged'}
"""

    def __init__(self, client: Redis, *, key_prefix: str = DEFAULT_KEY_PREFIX) -> None:
        self.client = client
        self.key_prefix = key_prefix.rstrip(":")
        self.consumer_group = CONSUMER_GROUP
        self._dispatch = client.register_script(self._dispatch_script)
        self._ack = client.register_script(self._ack_script)

    @classmethod
    def from_url(cls, url: str | None = None, *, key_prefix: str | None = None) -> RedisMessageQueue:
        resolved_url = url or os.environ.get("MINI_AGENT_REDIS_URL", DEFAULT_REDIS_URL)
        client = Redis.from_url(resolved_url, decode_responses=True, socket_connect_timeout=1, socket_timeout=2)
        return cls(client, key_prefix=key_prefix or os.environ.get("MINI_AGENT_REDIS_KEY_PREFIX", DEFAULT_KEY_PREFIX))

    def _thread_keys(self, thread_id: str) -> tuple[str, str, str]:
        base = f"{self.key_prefix}:thread:{thread_id}:queued"
        return f"{base}:order", f"{base}:messages", f"{base}:sequence"

    def _stream_key(self, turn_id: str) -> str:
        return f"{self.key_prefix}:turn:{turn_id}:mailbox"

    def _receipt_key(self, delivery_id: str) -> str:
        return f"{self.key_prefix}:delivery:{delivery_id}"

    @staticmethod
    def _unavailable(exc: BaseException) -> MessageQueueUnavailable:
        return MessageQueueUnavailable("message_queue_unavailable")

    def ping(self) -> None:
        try:
            self.client.ping()
        except RedisError as exc:
            raise self._unavailable(exc) from exc

    def close(self) -> None:
        self.client.close()

    def list(self, thread_id: str) -> list[QueuedMessage]:
        order, messages, _ = self._thread_keys(thread_id)
        try:
            ids = self.client.zrange(order, 0, -1)
            if not ids:
                return []
            raw = self.client.hmget(messages, ids)
        except RedisError as exc:
            raise self._unavailable(exc) from exc
        return [QueuedMessage.from_dict(json.loads(value)) for value in raw if value]

    def create(self, item: QueuedMessage) -> tuple[QueuedMessage, bool]:
        order, messages, sequence = self._thread_keys(item.thread_id)
        payload = _json(item.to_dict())
        try:
            with self.client.pipeline() as pipe:
                while True:
                    try:
                        pipe.watch(messages)
                        existing = pipe.hget(messages, item.id)
                        if existing is not None:
                            current = QueuedMessage.from_dict(json.loads(existing))
                            if _same_create(current, item):
                                return current, False
                            raise QueueItemConflict("queued_message_id_conflict")
                        next_sequence = int(pipe.get(sequence) or 0) + 1
                        pipe.multi()
                        pipe.set(sequence, next_sequence)
                        pipe.hset(messages, item.id, payload)
                        pipe.zadd(order, {item.id: next_sequence})
                        pipe.execute()
                        return item, True
                    except RedisError as exc:
                        if exc.__class__.__name__ == "WatchError":
                            continue
                        raise
        except (QueueItemConflict, QueueItemStateConflict):
            raise
        except RedisError as exc:
            raise self._unavailable(exc) from exc

    def update(
        self, thread_id: str, message_id: str, *, content: str, references: Sequence[dict[str, str]]
    ) -> QueuedMessage:
        _, messages, _ = self._thread_keys(thread_id)
        try:
            with self.client.pipeline() as pipe:
                while True:
                    try:
                        pipe.watch(messages)
                        raw = pipe.hget(messages, message_id)
                        if raw is None:
                            raise QueueItemNotFound("queued_message_not_found")
                        current = QueuedMessage.from_dict(json.loads(raw))
                        if current.state != "pending":
                            raise QueueItemStateConflict("queued_message_dispatched")
                        updated = replace(
                            current,
                            content=content,
                            references=tuple(dict(value) for value in references),
                            updated_at=queue_utc_now(),
                        )
                        pipe.multi()
                        pipe.hset(messages, message_id, _json(updated.to_dict()))
                        pipe.execute()
                        return updated
                    except RedisError as exc:
                        if exc.__class__.__name__ == "WatchError":
                            continue
                        raise
        except (QueueItemNotFound, QueueItemStateConflict):
            raise
        except RedisError as exc:
            raise self._unavailable(exc) from exc

    def delete(self, thread_id: str, message_id: str) -> None:
        order, messages, _ = self._thread_keys(thread_id)
        try:
            with self.client.pipeline() as pipe:
                while True:
                    try:
                        pipe.watch(messages)
                        raw = pipe.hget(messages, message_id)
                        if raw is None:
                            raise QueueItemNotFound("queued_message_not_found")
                        current = QueuedMessage.from_dict(json.loads(raw))
                        if current.state != "pending":
                            raise QueueItemStateConflict("queued_message_dispatched")
                        pipe.multi()
                        pipe.hdel(messages, message_id)
                        pipe.zrem(order, message_id)
                        pipe.execute()
                        return
                    except RedisError as exc:
                        if exc.__class__.__name__ == "WatchError":
                            continue
                        raise
        except (QueueItemNotFound, QueueItemStateConflict):
            raise
        except RedisError as exc:
            raise self._unavailable(exc) from exc

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
        if not message_ids or len(set(message_ids)) != len(message_ids):
            raise QueueItemConflict("message_ids_must_be_unique")
        requested_fingerprint = _fingerprint(thread_id, turn_id, message_ids)
        receipt_key = self._receipt_key(delivery_id)
        try:
            receipt = self.client.hgetall(receipt_key)
        except RedisError as exc:
            raise self._unavailable(exc) from exc
        stored_envelope: MessageEnvelope | None = None
        if receipt:
            if receipt.get("fingerprint") != requested_fingerprint:
                raise DeliveryConflict("delivery_id_conflict")
            raw_envelope = receipt.get("envelope")
            if raw_envelope:
                stored_envelope = MessageEnvelope.from_dict(json.loads(raw_envelope))
                if receipt.get("status") != "returned":
                    return stored_envelope
        order, messages_key, _ = self._thread_keys(thread_id)
        try:
            ordered_ids = self.client.zrange(order, 0, -1)
            selected = set(message_ids)
            canonical_ids = [item for item in ordered_ids if item in selected]
            raw = self.client.hmget(messages_key, canonical_ids)
        except RedisError as exc:
            raise self._unavailable(exc) from exc
        if len(canonical_ids) != len(message_ids) or any(value is None for value in raw):
            raise QueueItemNotFound("queued_message_not_found")
        queued = [QueuedMessage.from_dict(json.loads(value)) for value in raw if value]
        if any(item.thread_id != thread_id for item in queued):
            raise QueueItemConflict("queued_message_thread_conflict")
        if any(item.state != "pending" for item in queued):
            raise QueueItemStateConflict("queued_message_dispatched")
        content, references = _merge(queued)
        envelope = stored_envelope or MessageEnvelope(
            delivery_id=delivery_id,
            sender_kind="user",
            source_thread_id=thread_id,
            target_kind="turn",
            target_id=turn_id,
            session_id=session_id,
            thread_id=thread_id,
            payload={"content": content, "references": [dict(value) for value in references]},
            source_message_ids=tuple(canonical_ids),
            correlation_id=correlation_id,
        )
        now = queue_utc_now()
        dispatched = [replace(item, state="dispatched", updated_at=now) for item in queued]
        fingerprint = _fingerprint(thread_id, turn_id, canonical_ids)
        arguments: list[object] = [fingerprint, len(canonical_ids), *canonical_ids]
        arguments.extend(value for value in raw if value is not None)
        arguments.extend(_json(item.to_dict()) for item in dispatched)
        arguments.append(_json(envelope.to_dict()))
        try:
            result = self._dispatch(keys=[receipt_key, messages_key, self._stream_key(turn_id)], args=arguments)
        except RedisError as exc:
            raise self._unavailable(exc) from exc
        code = str(result[0])
        if code == "delivery_conflict":
            raise DeliveryConflict("delivery_id_conflict")
        if code == "missing":
            raise QueueItemNotFound("queued_message_not_found")
        if code == "state_conflict":
            raise QueueItemStateConflict("queued_message_dispatched")
        if code == "queue_changed":
            raise QueueItemConflict("queued_message_changed_during_dispatch")
        return envelope

    def _ensure_group(self, stream: str) -> None:
        try:
            self.client.xgroup_create(stream, self.consumer_group, id="0-0", mkstream=True)
        except RedisError as exc:
            if "BUSYGROUP" not in str(exc):
                raise self._unavailable(exc) from exc

    def claim(self, turn_id: str, consumer: str) -> ClaimedEnvelope | None:
        stream = self._stream_key(turn_id)
        self._ensure_group(stream)
        try:
            reclaimed = self.client.xautoclaim(
                stream, self.consumer_group, consumer, min_idle_time=STALE_CLAIM_MS, start_id="0-0", count=1
            )
            entries = reclaimed[1] if len(reclaimed) > 1 else []
            if not entries:
                pending = self.client.xpending(stream, self.consumer_group)
                if int(pending.get("pending", 0)) > 0:
                    return None
                response = self.client.xreadgroup(self.consumer_group, consumer, {stream: ">"}, count=1, block=None)
                entries = response[0][1] if response else []
            if not entries:
                return None
            stream_id, fields = entries[0]
            envelope = MessageEnvelope.from_dict(json.loads(fields["envelope"]))
            attempts = int(self.client.hincrby(self._receipt_key(envelope.delivery_id), "attempts", 1))
            return ClaimedEnvelope(str(stream_id), replace(envelope, attempts=attempts))
        except RedisError as exc:
            raise self._unavailable(exc) from exc

    def ack(self, claimed: ClaimedEnvelope) -> None:
        envelope = claimed.envelope
        order, messages, _ = self._thread_keys(envelope.thread_id)
        fingerprint = _fingerprint(envelope.thread_id, envelope.target_id, envelope.source_message_ids)
        arguments: list[object] = [
            fingerprint,
            self.consumer_group,
            claimed.stream_id,
            len(envelope.source_message_ids),
            *envelope.source_message_ids,
            queue_utc_now(),
            DELIVERY_RECEIPT_TTL_SECONDS,
        ]
        try:
            result = self._ack(
                keys=[self._receipt_key(envelope.delivery_id), self._stream_key(envelope.target_id), messages, order],
                args=arguments,
            )
        except RedisError as exc:
            raise self._unavailable(exc) from exc
        if str(result[0]) == "delivery_conflict":
            raise DeliveryConflict("delivery_id_conflict")

    def release_turn(self, turn_id: str) -> None:
        stream = self._stream_key(turn_id)
        try:
            entries = self.client.xrange(stream)
            if entries:
                self._ensure_group(stream)
            for stream_id, fields in entries:
                envelope = MessageEnvelope.from_dict(json.loads(fields["envelope"]))
                order, messages, _ = self._thread_keys(envelope.thread_id)
                with self.client.pipeline(transaction=True) as pipe:
                    for message_id in envelope.source_message_ids:
                        raw = self.client.hget(messages, message_id)
                        if raw:
                            item = QueuedMessage.from_dict(json.loads(raw))
                            pipe.hset(
                                messages,
                                message_id,
                                _json(replace(item, state="pending", updated_at=queue_utc_now()).to_dict()),
                            )
                    pipe.xack(stream, self.consumer_group, stream_id)
                    pipe.xdel(stream, stream_id)
                    pipe.hset(self._receipt_key(envelope.delivery_id), mapping={"status": "returned"})
                    pipe.expire(self._receipt_key(envelope.delivery_id), DELIVERY_RECEIPT_TTL_SECONDS)
                    pipe.execute()
                del order
            if self.client.xlen(stream) == 0:
                self.client.delete(stream)
        except RedisError as exc:
            raise self._unavailable(exc) from exc

    def pending_deliveries(self) -> list[ClaimedEnvelope]:
        pattern = f"{self.key_prefix}:turn:*:mailbox"
        result: list[ClaimedEnvelope] = []
        try:
            for stream in self.client.scan_iter(pattern):
                for stream_id, fields in self.client.xrange(stream):
                    raw = fields.get("envelope")
                    if raw:
                        result.append(ClaimedEnvelope(str(stream_id), MessageEnvelope.from_dict(json.loads(raw))))
        except RedisError as exc:
            raise self._unavailable(exc) from exc
        return result


class MemoryMessageQueue:
    """Injectable deterministic test port; real Redis coverage lives in integration tests."""

    def __init__(self) -> None:
        self._queues: dict[str, list[QueuedMessage]] = {}
        self._streams: dict[str, list[tuple[str, MessageEnvelope, float, str | None]]] = {}
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

    def claim(self, turn_id: str, consumer: str) -> ClaimedEnvelope | None:
        with self._lock:
            entries = self._streams.get(turn_id, [])
            for index, (stream_id, envelope, claimed_at, owner) in enumerate(entries):
                if owner is None:
                    claimed = replace(envelope, attempts=envelope.attempts + 1)
                    entries[index] = (stream_id, claimed, claimed_at + 1, consumer)
                    return ClaimedEnvelope(stream_id, claimed)
            return None

    def ack(self, claimed: ClaimedEnvelope) -> None:
        with self._lock:
            envelope = claimed.envelope
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
                for entries in self._streams.values()
                for stream_id, envelope, _, _ in entries
            ]


class RedisTurnMailbox:
    """Callable safe-boundary adapter consumed by AgentRuntime."""

    def __init__(self, queue: RedisMessageQueue | MemoryMessageQueue, turn_id: str, consumer: str) -> None:
        self.queue = queue
        self.turn_id = turn_id
        self.consumer = consumer
        self.closed = False

    def take(self) -> list[dict[str, Any]]:
        if self.closed:
            return []
        claimed = self.queue.claim(self.turn_id, self.consumer)
        if claimed is None:
            return []
        envelope = claimed.envelope
        return [
            {
                "delivery_id": envelope.delivery_id,
                "content": envelope.content,
                "references": list(envelope.references),
                "_ack": lambda: self.queue.ack(claimed),
            }
        ]

    def close(self) -> None:
        self.closed = True


__all__ = [
    "DEFAULT_KEY_PREFIX",
    "DEFAULT_REDIS_URL",
    "MemoryMessageQueue",
    "RedisMessageQueue",
    "RedisTurnMailbox",
]
