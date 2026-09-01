"""Redis implementation for editable queued messages and durable deliveries."""

from __future__ import annotations

import json
import os
from collections.abc import Sequence
from dataclasses import replace

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

from .message_queue_support import (
    CONSUMER_GROUP,
    DEFAULT_KEY_PREFIX,
    DEFAULT_REDIS_URL,
    DELIVERY_RECEIPT_TTL_SECONDS,
    STALE_CLAIM_MS,
    _envelope_fingerprint,
    _fingerprint,
    _json,
    _merge,
    _same_create,
)
from .redis_message_scripts import ACK_SCRIPT, DIRECT_ACK_SCRIPT, DIRECT_DISPATCH_SCRIPT, DISPATCH_SCRIPT


class RedisMessageQueue:
    """Versioned Redis keyspace for editable drafts and immutable deliveries."""

    _dispatch_script = DISPATCH_SCRIPT
    _ack_script = ACK_SCRIPT
    _direct_dispatch_script = DIRECT_DISPATCH_SCRIPT
    _direct_ack_script = DIRECT_ACK_SCRIPT

    def __init__(self, client: Redis, *, key_prefix: str = DEFAULT_KEY_PREFIX) -> None:
        self.client = client
        self.key_prefix = key_prefix.rstrip(":")
        self.consumer_group = CONSUMER_GROUP
        self._dispatch = client.register_script(self._dispatch_script)
        self._ack = client.register_script(self._ack_script)
        self._direct_dispatch = client.register_script(self._direct_dispatch_script)
        self._direct_ack = client.register_script(self._direct_ack_script)

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

    def _thread_stream_key(self, thread_id: str) -> str:
        return f"{self.key_prefix}:thread:{thread_id}:mailbox"

    def _report_stream_key(self, thread_id: str) -> str:
        return f"{self.key_prefix}:thread:{thread_id}:assistant-reports"

    def _envelope_stream_key(self, envelope: MessageEnvelope) -> str:
        if envelope.target_kind == "thread":
            return self._thread_stream_key(envelope.target_id)
        if envelope.target_kind == "report":
            return self._report_stream_key(envelope.target_id)
        return self._stream_key(envelope.target_id)

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

    def dispatch_agent(self, envelope: MessageEnvelope) -> MessageEnvelope:
        """Atomically append one immutable Agent envelope to a Thread mailbox."""

        if envelope.sender_kind != "agent" or envelope.target_kind != "thread":
            raise ValueError("Agent dispatch requires sender_kind=agent and target_kind=thread.")
        fingerprint = _envelope_fingerprint(envelope)
        receipt = self._receipt_key(envelope.delivery_id)
        try:
            result = self._direct_dispatch(
                keys=[receipt, self._thread_stream_key(envelope.target_id)],
                args=[fingerprint, _json(replace(envelope, attempts=0).to_dict())],
            )
        except RedisError as exc:
            raise self._unavailable(exc) from exc
        if str(result[0]) == "delivery_conflict":
            raise DeliveryConflict("delivery_id_conflict")
        return envelope

    def dispatch_report(self, envelope: MessageEnvelope) -> MessageEnvelope:
        """Atomically append one immutable report to the Assistant-report stream."""

        if envelope.sender_kind != "agent" or envelope.target_kind != "report":
            raise ValueError("Report dispatch requires sender_kind=agent and target_kind=report.")
        fingerprint = _envelope_fingerprint(envelope)
        receipt = self._receipt_key(envelope.delivery_id)
        try:
            result = self._direct_dispatch(
                keys=[receipt, self._report_stream_key(envelope.target_id)],
                args=[fingerprint, _json(replace(envelope, attempts=0).to_dict())],
            )
        except RedisError as exc:
            raise self._unavailable(exc) from exc
        if str(result[0]) == "delivery_conflict":
            raise DeliveryConflict("delivery_id_conflict")
        return envelope

    def _ensure_group(self, stream: str) -> None:
        try:
            self.client.xgroup_create(stream, self.consumer_group, id="0-0", mkstream=True)
        except RedisError as exc:
            if "BUSYGROUP" not in str(exc):
                raise self._unavailable(exc) from exc

    def _claim_stream(
        self,
        stream: str,
        consumer: str,
        *,
        stale_claim_ms: int = STALE_CLAIM_MS,
    ) -> ClaimedEnvelope | None:
        self._ensure_group(stream)
        try:
            reclaimed = self.client.xautoclaim(
                stream,
                self.consumer_group,
                consumer,
                min_idle_time=stale_claim_ms,
                start_id="0-0",
                count=1,
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

    def claim(self, turn_id: str, consumer: str) -> ClaimedEnvelope | None:
        return self._claim_stream(self._stream_key(turn_id), consumer)

    def claim_thread(self, thread_id: str, consumer: str) -> ClaimedEnvelope | None:
        return self._claim_stream(self._thread_stream_key(thread_id), consumer)

    def claim_thread_recovery(self, thread_id: str, consumer: str) -> ClaimedEnvelope | None:
        return self._claim_stream(self._thread_stream_key(thread_id), consumer, stale_claim_ms=0)

    def claim_report(self, thread_id: str, consumer: str, *, recover: bool = False) -> ClaimedEnvelope | None:
        return self._claim_stream(
            self._report_stream_key(thread_id),
            consumer,
            stale_claim_ms=0 if recover else STALE_CLAIM_MS,
        )

    def peek_thread(self, thread_id: str) -> MessageEnvelope | None:
        try:
            entries = self.client.xrange(self._thread_stream_key(thread_id), count=1)
        except RedisError as exc:
            raise self._unavailable(exc) from exc
        if not entries:
            return None
        raw = entries[0][1].get("envelope")
        return MessageEnvelope.from_dict(json.loads(raw)) if raw else None

    def ack(self, claimed: ClaimedEnvelope) -> None:
        envelope = claimed.envelope
        if envelope.target_kind in {"thread", "report"}:
            arguments: list[object] = [
                _envelope_fingerprint(envelope),
                self.consumer_group,
                claimed.stream_id,
                queue_utc_now(),
                DELIVERY_RECEIPT_TTL_SECONDS,
            ]
            try:
                result = self._direct_ack(
                    keys=[self._receipt_key(envelope.delivery_id), self._envelope_stream_key(envelope)],
                    args=arguments,
                )
            except RedisError as exc:
                raise self._unavailable(exc) from exc
            if str(result[0]) == "delivery_conflict":
                raise DeliveryConflict("delivery_id_conflict")
            return
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
        result: list[ClaimedEnvelope] = []
        try:
            patterns = (
                f"{self.key_prefix}:turn:*:mailbox",
                f"{self.key_prefix}:thread:*:mailbox",
                f"{self.key_prefix}:thread:*:assistant-reports",
            )
            for pattern in patterns:
                for stream in self.client.scan_iter(pattern):
                    for stream_id, fields in self.client.xrange(stream):
                        raw = fields.get("envelope")
                        if raw:
                            result.append(ClaimedEnvelope(str(stream_id), MessageEnvelope.from_dict(json.loads(raw))))
        except RedisError as exc:
            raise self._unavailable(exc) from exc
        return result


__all__ = [
    "DEFAULT_KEY_PREFIX",
    "DEFAULT_REDIS_URL",
    "DELIVERY_RECEIPT_TTL_SECONDS",
    "STALE_CLAIM_MS",
    "RedisMessageQueue",
]
