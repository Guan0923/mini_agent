"""Process-local browser event streams for persistent Subagent Threads."""

from __future__ import annotations

import asyncio
import json
import queue
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from threading import RLock
from time import monotonic

from backend.domain.runtime_state import NodeFrame, RuntimeState


def _sse(payload: dict[str, object]) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}\n\n"


@dataclass(slots=True)
class AgentThreadSubscription:
    hub: AgentThreadEventHub
    key: tuple[str, str]
    token: int
    events: queue.Queue[dict[str, object]] = field(default_factory=queue.Queue)
    closed: bool = False

    def next_event(self, timeout: float = 0.5) -> dict[str, object]:
        return self.events.get(timeout=timeout)

    async def as_sse(self) -> AsyncIterator[str]:
        heartbeat_at = monotonic() + 15.0
        try:
            while not self.closed:
                try:
                    event = self.events.get_nowait()
                except queue.Empty:
                    now = monotonic()
                    if now >= heartbeat_at:
                        heartbeat_at = now + 15.0
                        yield ": heartbeat\n\n"
                    await asyncio.sleep(0.05)
                    continue
                yield _sse(event)
        finally:
            self.hub.unsubscribe(self.key, self.token)


@dataclass(slots=True)
class _ThreadChannel:
    next_token: int = 0
    latest: RuntimeState | None = None
    subscribers: dict[int, AgentThreadSubscription] = field(default_factory=dict)


class AgentThreadEventHub:
    """Keep one long-lived event channel per Agent Thread across its Turns."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._channels: dict[tuple[str, str], _ThreadChannel] = {}

    def subscribe(self, session_id: str, thread_id: str) -> AgentThreadSubscription:
        key = (session_id, thread_id)
        with self._lock:
            channel = self._channels.setdefault(key, _ThreadChannel())
            token = channel.next_token
            channel.next_token += 1
            subscription = AgentThreadSubscription(self, key, token)
            channel.subscribers[token] = subscription
            subscription.events.put({"type": "thread.ready", "session_id": session_id, "thread_id": thread_id})
            if channel.latest is not None:
                subscription.events.put(NodeFrame.snapshot(channel.latest).to_dict())
            return subscription

    def unsubscribe(self, key: tuple[str, str], token: int) -> None:
        with self._lock:
            channel = self._channels.get(key)
            subscription = channel.subscribers.pop(token, None) if channel is not None else None
            if subscription is not None:
                subscription.closed = True

    def start_turn(self, turn: RuntimeState) -> None:
        self._publish(turn.thread_id, NodeFrame.snapshot(turn), turn)

    def publish_frame(self, thread_id: str, frame: NodeFrame, current: RuntimeState) -> None:
        self._publish(thread_id, frame, current)

    def _publish(self, thread_id: str, frame: NodeFrame, current: RuntimeState) -> None:
        key = (current.session_id, thread_id)
        payload = frame.to_dict()
        with self._lock:
            channel = self._channels.setdefault(key, _ThreadChannel())
            channel.latest = current.clone()
            for subscription in channel.subscribers.values():
                subscription.events.put(payload)

    def finish_turn(self, thread_id: str, turn: RuntimeState) -> None:
        key = (turn.session_id, thread_id)
        terminal = {
            "type": "turn.terminal",
            "session_id": turn.session_id,
            "thread_id": thread_id,
            "turn_id": turn.id,
            "status": turn.status,
        }
        with self._lock:
            channel = self._channels.setdefault(key, _ThreadChannel())
            channel.latest = turn.clone()
            for subscription in channel.subscribers.values():
                subscription.events.put(terminal)

    def close(self) -> None:
        with self._lock:
            for channel in self._channels.values():
                for subscription in channel.subscribers.values():
                    subscription.closed = True
                channel.subscribers.clear()
            self._channels.clear()


__all__ = ["AgentThreadEventHub", "AgentThreadSubscription"]
