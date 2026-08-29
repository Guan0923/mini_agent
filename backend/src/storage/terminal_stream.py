"""Short-lived Redis output streams for interactive terminals."""

from __future__ import annotations

from dataclasses import dataclass

from redis import Redis
from redis.exceptions import RedisError

from backend.domain import MessageQueueUnavailable

MAX_TERMINAL_CHUNK_BYTES = 16 * 1024
MAX_TERMINAL_CHUNKS = 256
TERMINAL_STREAM_TTL_SECONDS = 30 * 60


@dataclass(frozen=True, slots=True)
class TerminalOutputChunk:
    sequence: int
    data: str


class RedisTerminalOutputStream:
    _append_script = """
local sequence = redis.call('INCR', KEYS[2])
redis.call('XADD', KEYS[1], '*', 'sequence', sequence, 'data', ARGV[1])
redis.call('XTRIM', KEYS[1], 'MAXLEN', '=', tonumber(ARGV[2]))
redis.call('PERSIST', KEYS[1])
redis.call('PERSIST', KEYS[2])
return sequence
"""

    def __init__(self, client: Redis, *, key_prefix: str) -> None:
        self.client = client
        self.key_prefix = key_prefix.rstrip(":")
        self._append = client.register_script(self._append_script)

    def _keys(self, terminal_id: str) -> tuple[str, str]:
        base = f"{self.key_prefix}:terminal:{terminal_id}"
        return f"{base}:output", f"{base}:sequence"

    @staticmethod
    def _unavailable(exc: BaseException) -> MessageQueueUnavailable:
        return MessageQueueUnavailable("message_queue_unavailable")

    def ping(self) -> None:
        try:
            self.client.ping()
        except RedisError as exc:
            raise self._unavailable(exc) from exc

    def append(self, terminal_id: str, data: str) -> list[TerminalOutputChunk]:
        encoded = data.encode("utf-8")
        chunks: list[TerminalOutputChunk] = []
        offset = 0
        try:
            while offset < len(encoded):
                end = min(offset + MAX_TERMINAL_CHUNK_BYTES, len(encoded))
                while end > offset:
                    try:
                        part = encoded[offset:end].decode("utf-8")
                        break
                    except UnicodeDecodeError:
                        end -= 1
                if end == offset:
                    end = min(offset + MAX_TERMINAL_CHUNK_BYTES, len(encoded))
                    part = encoded[offset:end].decode("utf-8", errors="replace")
                stream, sequence = self._keys(terminal_id)
                value = self._append(keys=[stream, sequence], args=[part, MAX_TERMINAL_CHUNKS])
                chunks.append(TerminalOutputChunk(int(value), part))
                offset = end
        except RedisError as exc:
            raise self._unavailable(exc) from exc
        return chunks

    def after(self, terminal_id: str, sequence: int) -> list[TerminalOutputChunk]:
        stream, _ = self._keys(terminal_id)
        try:
            rows = self.client.xrange(stream, min="-", max="+")
        except RedisError as exc:
            raise self._unavailable(exc) from exc
        result: list[TerminalOutputChunk] = []
        for _, fields in rows:
            item_sequence = int(fields.get("sequence", 0))
            if item_sequence > sequence:
                result.append(TerminalOutputChunk(item_sequence, str(fields.get("data", ""))))
        return result

    def expire(self, terminal_id: str) -> None:
        stream, sequence = self._keys(terminal_id)
        try:
            with self.client.pipeline() as pipe:
                pipe.expire(stream, TERMINAL_STREAM_TTL_SECONDS)
                pipe.expire(sequence, TERMINAL_STREAM_TTL_SECONDS)
                pipe.execute()
        except RedisError as exc:
            raise self._unavailable(exc) from exc

    def delete(self, terminal_id: str) -> None:
        stream, sequence = self._keys(terminal_id)
        try:
            self.client.delete(stream, sequence)
        except RedisError as exc:
            raise self._unavailable(exc) from exc


__all__ = [
    "MAX_TERMINAL_CHUNK_BYTES",
    "MAX_TERMINAL_CHUNKS",
    "RedisTerminalOutputStream",
    "TERMINAL_STREAM_TTL_SECONDS",
    "TerminalOutputChunk",
]
