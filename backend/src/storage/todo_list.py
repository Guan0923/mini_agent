"""Redis-authoritative, Turn-scoped Todo storage."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from threading import RLock
from typing import Any
from uuid import uuid4

from redis import Redis
from redis.exceptions import RedisError, WatchError

from backend.domain import MessageQueueUnavailable
from backend.domain.todo import TodoSnapshot, TodoStateError, TodoUpdateResult, apply_todo_operations

TODO_TTL_SECONDS = 24 * 60 * 60
_MAX_TRANSACTION_RETRIES = 16


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _fingerprint(expected_revision: int, operations: Sequence[Mapping[str, Any]]) -> str:
    payload = _json({"expected_revision": expected_revision, "operations": list(operations)})
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class RedisTodoListStore:
    """Store one authoritative Todo state and its idempotency receipts per Turn."""

    def __init__(self, client: Redis, *, key_prefix: str) -> None:
        self.client = client
        self.key_prefix = key_prefix.rstrip(":")

    def _key(self, session_id: str, turn_id: str) -> str:
        return f"{self.key_prefix}:session:{session_id}:turn:{turn_id}:todo"

    @staticmethod
    def _receipt_field(call_id: str) -> str:
        return f"receipt:{call_id}"

    @staticmethod
    def _unavailable(exc: BaseException) -> MessageQueueUnavailable:
        return MessageQueueUnavailable("message_queue_unavailable")

    @staticmethod
    def _snapshot(raw: Mapping[str, str]) -> TodoSnapshot:
        value = raw.get("snapshot")
        if not value:
            return TodoSnapshot()
        decoded = json.loads(value)
        if not isinstance(decoded, Mapping):
            raise ValueError("Stored Todo snapshot is invalid.")
        return TodoSnapshot.from_dict(decoded)

    def update(
        self,
        *,
        session_id: str,
        turn_id: str,
        call_id: str,
        expected_revision: int,
        operations: Sequence[Mapping[str, Any]],
    ) -> TodoUpdateResult:
        if not isinstance(expected_revision, int) or isinstance(expected_revision, bool) or expected_revision < 0:
            raise TodoStateError("invalid_revision", "Expected revision must be a non-negative integer.")
        key = self._key(session_id, turn_id)
        receipt_field = self._receipt_field(call_id)
        fingerprint = _fingerprint(expected_revision, operations)
        generated_ids = tuple(f"todo_{uuid4().hex}" for operation in operations if operation.get("op") == "add")
        try:
            with self.client.pipeline() as pipe:
                for _attempt in range(_MAX_TRANSACTION_RETRIES):
                    try:
                        pipe.watch(key)
                        raw = pipe.hgetall(key)
                        existing = raw.get(receipt_field)
                        if existing:
                            receipt = json.loads(existing)
                            if receipt.get("fingerprint") != fingerprint:
                                raise TodoStateError(
                                    "call_id_conflict",
                                    f"Call ID {call_id!r} was already used with different arguments.",
                                )
                            return TodoUpdateResult.from_dict(receipt["result"])
                        current = self._snapshot(raw)
                        if current.revision != expected_revision:
                            raise TodoStateError(
                                "revision_conflict",
                                f"Expected revision {expected_revision}, current revision is {current.revision}.",
                                snapshot=current,
                            )
                        updated, applied = apply_todo_operations(
                            current,
                            operations,
                            generated_ids=generated_ids,
                        )
                        result = TodoUpdateResult(turn_id, updated, applied)
                        pipe.multi()
                        pipe.hset(
                            key,
                            mapping={
                                "session_id": session_id,
                                "turn_id": turn_id,
                                "revision": updated.revision,
                                "snapshot": _json(updated.to_dict()),
                                receipt_field: _json({"fingerprint": fingerprint, "result": result.to_dict()}),
                            },
                        )
                        pipe.persist(key)
                        pipe.execute()
                        return result
                    except WatchError:
                        continue
                raise TodoStateError(
                    "concurrent_update", "Todo list changed too frequently; retry with a new snapshot."
                )
        except TodoStateError:
            raise
        except (RedisError, TypeError, ValueError) as exc:
            raise self._unavailable(exc) from exc

    def snapshot(self, session_id: str, turn_id: str) -> TodoSnapshot:
        try:
            return self._snapshot(self.client.hgetall(self._key(session_id, turn_id)))
        except (RedisError, TypeError, ValueError) as exc:
            raise self._unavailable(exc) from exc

    def receipt(self, session_id: str, turn_id: str, call_id: str) -> TodoUpdateResult | None:
        try:
            raw = self.client.hget(self._key(session_id, turn_id), self._receipt_field(call_id))
            if not raw:
                return None
            decoded = json.loads(raw)
            return TodoUpdateResult.from_dict(decoded["result"])
        except (RedisError, KeyError, TypeError, ValueError) as exc:
            raise self._unavailable(exc) from exc

    def claim_finalization(self, session_id: str, turn_id: str) -> bool:
        key = self._key(session_id, turn_id)
        try:
            with self.client.pipeline() as pipe:
                for _attempt in range(_MAX_TRANSACTION_RETRIES):
                    try:
                        pipe.watch(key)
                        raw = pipe.hgetall(key)
                        snapshot = self._snapshot(raw)
                        if not snapshot.unfinished or raw.get("finalization_claimed") == "1":
                            return False
                        pipe.multi()
                        pipe.hset(key, "finalization_claimed", "1")
                        pipe.persist(key)
                        pipe.execute()
                        return True
                    except WatchError:
                        continue
                raise TodoStateError("concurrent_update", "Todo finalization state changed too frequently.")
        except TodoStateError:
            raise
        except (RedisError, TypeError, ValueError) as exc:
            raise self._unavailable(exc) from exc

    def finalization_claimed(self, session_id: str, turn_id: str) -> bool:
        try:
            return self.client.hget(self._key(session_id, turn_id), "finalization_claimed") == "1"
        except RedisError as exc:
            raise self._unavailable(exc) from exc

    def persist_turn(self, session_id: str, turn_id: str) -> None:
        try:
            self.client.persist(self._key(session_id, turn_id))
        except RedisError as exc:
            raise self._unavailable(exc) from exc

    def expire_turn(self, session_id: str, turn_id: str) -> None:
        try:
            self.client.expire(self._key(session_id, turn_id), TODO_TTL_SECONDS)
        except RedisError as exc:
            raise self._unavailable(exc) from exc


class MemoryTodoListStore:
    """Explicit test adapter with the same observable contract as Redis storage."""

    def __init__(self) -> None:
        self._states: dict[tuple[str, str], TodoSnapshot] = {}
        self._receipts: dict[tuple[str, str, str], tuple[str, TodoUpdateResult]] = {}
        self._finalization: set[tuple[str, str]] = set()
        self._lock = RLock()

    def update(
        self,
        *,
        session_id: str,
        turn_id: str,
        call_id: str,
        expected_revision: int,
        operations: Sequence[Mapping[str, Any]],
    ) -> TodoUpdateResult:
        if not isinstance(expected_revision, int) or isinstance(expected_revision, bool) or expected_revision < 0:
            raise TodoStateError("invalid_revision", "Expected revision must be a non-negative integer.")
        fingerprint = _fingerprint(expected_revision, operations)
        receipt_key = (session_id, turn_id, call_id)
        with self._lock:
            receipt = self._receipts.get(receipt_key)
            if receipt is not None:
                if receipt[0] != fingerprint:
                    raise TodoStateError("call_id_conflict", "Call ID was already used with different arguments.")
                return receipt[1]
            current = self._states.get((session_id, turn_id), TodoSnapshot())
            if current.revision != expected_revision:
                raise TodoStateError(
                    "revision_conflict",
                    f"Expected revision {expected_revision}, current revision is {current.revision}.",
                    snapshot=current,
                )
            generated = tuple(f"todo_{uuid4().hex}" for operation in operations if operation.get("op") == "add")
            updated, applied = apply_todo_operations(current, operations, generated_ids=generated)
            result = TodoUpdateResult(turn_id, updated, applied)
            self._states[(session_id, turn_id)] = updated
            self._receipts[receipt_key] = (fingerprint, result)
            return result

    def snapshot(self, session_id: str, turn_id: str) -> TodoSnapshot:
        with self._lock:
            return self._states.get((session_id, turn_id), TodoSnapshot())

    def receipt(self, session_id: str, turn_id: str, call_id: str) -> TodoUpdateResult | None:
        with self._lock:
            receipt = self._receipts.get((session_id, turn_id, call_id))
            return receipt[1] if receipt is not None else None

    def claim_finalization(self, session_id: str, turn_id: str) -> bool:
        key = (session_id, turn_id)
        with self._lock:
            if not self.snapshot(session_id, turn_id).unfinished or key in self._finalization:
                return False
            self._finalization.add(key)
            return True

    def finalization_claimed(self, session_id: str, turn_id: str) -> bool:
        with self._lock:
            return (session_id, turn_id) in self._finalization

    def persist_turn(self, session_id: str, turn_id: str) -> None:
        return None

    def expire_turn(self, session_id: str, turn_id: str) -> None:
        return None


__all__ = ["MemoryTodoListStore", "RedisTodoListStore", "TODO_TTL_SECONDS"]
