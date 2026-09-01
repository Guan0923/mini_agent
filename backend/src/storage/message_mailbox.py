"""Safe-boundary mailbox adapters consumed by AgentRuntime."""

from __future__ import annotations

from typing import Any

from .memory_message_queue import MemoryMessageQueue
from .redis_message_queue import RedisMessageQueue


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
                "source_thread_id": envelope.source_thread_id,
                "need_reply": bool(envelope.payload.get("need_reply", False)),
                "_ack": lambda: self.queue.ack(claimed),
            }
        ]

    def close(self) -> None:
        self.closed = True


class RedisAgentMailbox(RedisTurnMailbox):
    """Safe-boundary adapter combining one Turn stream and its Thread mailbox."""

    def __init__(
        self,
        queue: RedisMessageQueue | MemoryMessageQueue,
        turn_id: str,
        thread_id: str,
        consumer: str,
    ) -> None:
        super().__init__(queue, turn_id, consumer)
        self.thread_id = thread_id

    def take(self) -> list[dict[str, Any]]:
        turn_items = super().take()
        if turn_items or self.closed:
            return turn_items
        claimed = self.queue.claim_thread(self.thread_id, self.consumer)
        if claimed is None:
            return []
        envelope = claimed.envelope
        return [
            {
                "delivery_id": envelope.delivery_id,
                "content": envelope.content,
                "references": list(envelope.references),
                "source_thread_id": envelope.source_thread_id,
                "need_reply": bool(envelope.payload.get("need_reply", False)),
                "_ack": lambda: self.queue.ack(claimed),
            }
        ]


__all__ = ["RedisAgentMailbox", "RedisTurnMailbox"]
