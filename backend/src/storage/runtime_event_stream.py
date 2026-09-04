"""Redis-backed fan-out streams for browser Runtime observation and replay."""

from __future__ import annotations

import json
from dataclasses import dataclass
from threading import RLock

from redis import Redis
from redis.exceptions import RedisError

from backend.domain import MessageQueueUnavailable

EVENT_STREAM_TTL_SECONDS = 24 * 60 * 60
EVENT_STREAM_MAXLEN = 10_000

_PUBLISH_SCRIPT = r"""
local existing = redis.call('GET', KEYS[1])
if existing then
  local ids = cjson.decode(existing)
  return {ids[1], ids[2]}
end
local turn_id = redis.call('XADD', KEYS[2], 'MAXLEN', ARGV[1], '*',
  'event_id', ARGV[2], 'sequence', ARGV[3], 'payload', ARGV[4])
local thread_id = redis.call('XADD', KEYS[3], 'MAXLEN', ARGV[1], '*',
  'event_id', ARGV[2], 'sequence', ARGV[3], 'turn_id', ARGV[5], 'payload', ARGV[4])
redis.call('EXPIRE', KEYS[2], ARGV[6])
redis.call('EXPIRE', KEYS[3], ARGV[6])
redis.call('SET', KEYS[1], cjson.encode({turn_id, thread_id}), 'EX', ARGV[7])
return {turn_id, thread_id}
"""


@dataclass(frozen=True, slots=True)
class RuntimeStreamEntry:
    stream_id: str
    event_id: str
    sequence: int
    payload: dict[str, object]


class RedisRuntimeEventStream:
    def __init__(self, client: Redis, *, key_prefix: str) -> None:
        self.client = client
        self.key_prefix = key_prefix.rstrip(":")
        self._publish = client.register_script(_PUBLISH_SCRIPT)

    def _turn_key(self, turn_id: str) -> str:
        return f"{self.key_prefix}:turn:{turn_id}:events"

    def _thread_key(self, thread_id: str) -> str:
        return f"{self.key_prefix}:thread:{thread_id}:events"

    def _receipt_key(self, event_id: str) -> str:
        return f"{self.key_prefix}:runtime-event:{event_id}"

    @staticmethod
    def _unavailable(exc: BaseException) -> MessageQueueUnavailable:
        return MessageQueueUnavailable("message_queue_unavailable")

    def publish(
        self,
        *,
        event_id: str,
        turn_id: str,
        thread_id: str,
        sequence: int,
        payload: dict[str, object],
    ) -> tuple[str, str]:
        try:
            result = self._publish(
                keys=[self._receipt_key(event_id), self._turn_key(turn_id), self._thread_key(thread_id)],
                args=[
                    EVENT_STREAM_MAXLEN,
                    event_id,
                    sequence,
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                    turn_id,
                    EVENT_STREAM_TTL_SECONDS,
                    7 * 24 * 60 * 60,
                ],
            )
        except RedisError as exc:
            raise self._unavailable(exc) from exc
        return str(result[0]), str(result[1])

    def latest_turn_id(self, turn_id: str) -> str:
        try:
            entries = self.client.xrevrange(self._turn_key(turn_id), count=1)
        except RedisError as exc:
            raise self._unavailable(exc) from exc
        return str(entries[0][0]) if entries else "0-0"

    def latest_turn_event(self, turn_id: str) -> RuntimeStreamEntry | None:
        """Return the newest retained event for one Turn without blocking."""

        try:
            entries = self.client.xrevrange(self._turn_key(turn_id), count=1)
        except RedisError as exc:
            raise self._unavailable(exc) from exc
        if not entries:
            return None
        stream_id, fields = entries[0]
        raw = fields.get("payload")
        payload = json.loads(raw) if raw else None
        if not isinstance(payload, dict):
            return None
        return RuntimeStreamEntry(
            str(stream_id),
            str(fields.get("event_id") or ""),
            int(fields.get("sequence") or 0),
            dict(payload),
        )

    def has_event(self, event_id: str) -> bool:
        try:
            return bool(self.client.exists(self._receipt_key(event_id)))
        except RedisError as exc:
            raise self._unavailable(exc) from exc

    def latest_thread_id(self, thread_id: str) -> str:
        try:
            entries = self.client.xrevrange(self._thread_key(thread_id), count=1)
        except RedisError as exc:
            raise self._unavailable(exc) from exc
        return str(entries[0][0]) if entries else "0-0"

    def read_turn(self, turn_id: str, after_id: str, *, block_ms: int = 1000) -> list[RuntimeStreamEntry]:
        return self._read(self._turn_key(turn_id), after_id, block_ms=block_ms)

    def read_thread(self, thread_id: str, after_id: str, *, block_ms: int = 1000) -> list[RuntimeStreamEntry]:
        return self._read(self._thread_key(thread_id), after_id, block_ms=block_ms)

    def _read(self, key: str, after_id: str, *, block_ms: int) -> list[RuntimeStreamEntry]:
        try:
            response = self.client.xread({key: after_id}, count=100, block=block_ms)
        except RedisError as exc:
            raise self._unavailable(exc) from exc
        result: list[RuntimeStreamEntry] = []
        for _stream, entries in response:
            for stream_id, fields in entries:
                raw = fields.get("payload")
                payload = json.loads(raw) if raw else None
                if not isinstance(payload, dict):
                    continue
                result.append(
                    RuntimeStreamEntry(
                        str(stream_id),
                        str(fields.get("event_id") or ""),
                        int(fields.get("sequence") or 0),
                        dict(payload),
                    )
                )
        return result


class MemoryRuntimeEventStream:
    """Deterministic fan-out stream for focused API tests."""

    def __init__(self) -> None:
        self._turns: dict[str, list[RuntimeStreamEntry]] = {}
        self._threads: dict[str, list[RuntimeStreamEntry]] = {}
        self._events: set[str] = set()
        self._counter = 0
        self._lock = RLock()

    def publish(
        self,
        *,
        event_id: str,
        turn_id: str,
        thread_id: str,
        sequence: int,
        payload: dict[str, object],
    ) -> tuple[str, str]:
        with self._lock:
            if event_id in self._events:
                existing = next(item for item in self._turns.get(turn_id, []) if item.event_id == event_id)
                return existing.stream_id, existing.stream_id
            self._counter += 1
            stream_id = f"{self._counter}-0"
            entry = RuntimeStreamEntry(stream_id, event_id, sequence, dict(payload))
            self._turns.setdefault(turn_id, []).append(entry)
            self._threads.setdefault(thread_id, []).append(entry)
            self._events.add(event_id)
            return stream_id, stream_id

    def latest_turn_id(self, turn_id: str) -> str:
        with self._lock:
            entries = self._turns.get(turn_id, [])
            return entries[-1].stream_id if entries else "0-0"

    def latest_turn_event(self, turn_id: str) -> RuntimeStreamEntry | None:
        """Return the newest retained event for one Turn without blocking."""

        with self._lock:
            entries = self._turns.get(turn_id, [])
            return entries[-1] if entries else None

    def has_event(self, event_id: str) -> bool:
        with self._lock:
            return event_id in self._events

    def latest_thread_id(self, thread_id: str) -> str:
        with self._lock:
            entries = self._threads.get(thread_id, [])
            return entries[-1].stream_id if entries else "0-0"

    @staticmethod
    def _after(entries: list[RuntimeStreamEntry], after_id: str) -> list[RuntimeStreamEntry]:
        if after_id == "0-0":
            return list(entries)
        return [entry for entry in entries if int(entry.stream_id.split("-", 1)[0]) > int(after_id.split("-", 1)[0])]

    def read_turn(self, turn_id: str, after_id: str, *, block_ms: int = 1000) -> list[RuntimeStreamEntry]:
        del block_ms
        with self._lock:
            return self._after(self._turns.get(turn_id, []), after_id)

    def read_thread(self, thread_id: str, after_id: str, *, block_ms: int = 1000) -> list[RuntimeStreamEntry]:
        del block_ms
        with self._lock:
            return self._after(self._threads.get(thread_id, []), after_id)


__all__ = [
    "EVENT_STREAM_MAXLEN",
    "EVENT_STREAM_TTL_SECONDS",
    "MemoryRuntimeEventStream",
    "RedisRuntimeEventStream",
    "RuntimeStreamEntry",
]
