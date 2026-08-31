"""Process-local browser event streams for persistent Subagent Threads."""

from __future__ import annotations

import asyncio
import json
import queue
from collections.abc import AsyncIterator, Callable
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
    source_bases: dict[tuple[str, str], int] = field(default_factory=dict)
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
    latest_source_revision: int = 0
    subscribers: dict[int, AgentThreadSubscription] = field(default_factory=dict)


class AgentThreadEventHub:
    """Keep one long-lived event channel per Agent Thread across its Turns."""

    def __init__(
        self,
        frame_projector: Callable[[NodeFrame, RuntimeState], dict[str, object]] | None = None,
    ) -> None:
        self._lock = RLock()
        self._channels: dict[tuple[str, str], _ThreadChannel] = {}
        self._frame_projector = frame_projector or (lambda frame, _current: frame.to_dict())

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
                snapshot = NodeFrame.snapshot(channel.latest)
                subscription.events.put(self._frame_projector(snapshot, channel.latest))
                subscription.source_bases[channel.latest.key] = channel.latest_source_revision
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
        channel_key = (current.session_id, thread_id)
        turn_key = (frame.session_id, frame.turn_id)
        with self._lock:
            channel = self._channels.setdefault(channel_key, _ThreadChannel())
            channel.latest = current.clone()
            channel.latest_source_revision = frame.revision
            for subscription in channel.subscribers.values():
                if frame.type == "turn.snapshot":
                    snapshot = NodeFrame.snapshot(current)
                    subscription.events.put(self._frame_projector(snapshot, current))
                    subscription.source_bases = {turn_key: frame.revision}
                    continue
                base = subscription.source_bases.get(turn_key)
                if base is None:
                    snapshot = NodeFrame.snapshot(current)
                    subscription.events.put(self._frame_projector(snapshot, current))
                    subscription.source_bases = {turn_key: frame.revision}
                    continue
                local_revision = frame.revision - base
                if local_revision <= 0:
                    continue
                local_frame = NodeFrame(
                    "turn.delta",
                    frame.session_id,
                    frame.turn_id,
                    local_revision,
                    patch=frame.patch,
                    operations=frame.operations,
                )
                subscription.events.put(self._frame_projector(local_frame, current))

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
