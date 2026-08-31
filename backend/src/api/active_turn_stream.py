"""Process-local broadcast streams for reconnecting to running Turns."""

from __future__ import annotations

import asyncio
import html
import json
import queue
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from threading import RLock

from backend.domain.runtime_state import NodeFrame, RuntimeState


def _terminal_sse(payload: dict[str, object]) -> str:
    terminal_id = html.escape(str(payload.get("terminal_id") or "unknown"), quote=True)
    terminal_type = html.escape(str(payload.get("terminal_type") or "failed"), quote=True)
    message = html.escape(str(payload.get("message") or ""), quote=False)
    return f'data: <SSE id="{terminal_id}" type="{terminal_type}">{message}</SSE>\n\n'


@dataclass
class ActiveTurnSubscription:
    """One independently rebased view of an active Turn stream."""

    stream: ActiveTurnStream
    token: int
    expected_turn_id: str
    events: queue.Queue[dict[str, object]] = field(default_factory=queue.Queue)
    source_bases: dict[tuple[str, str], int] = field(default_factory=dict)
    closed: bool = False

    def next_event(self, timeout: float = 0.5) -> dict[str, object]:
        """Return the next normalized event for focused contract tests."""

        return self.events.get(timeout=timeout)

    async def as_sse(self) -> AsyncIterator[str]:
        try:
            while True:
                try:
                    event = self.events.get_nowait()
                except queue.Empty:
                    if self.closed:
                        break
                    await asyncio.sleep(0.05)
                    continue
                if event.get("type") == "terminal":
                    yield _terminal_sse(event)
                    break
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        finally:
            self.stream.unsubscribe(self.token)


class ActiveTurnStream:
    """Broadcast one runtime's frames while rebasing each late subscriber."""

    def __init__(
        self,
        turn_id: str,
        frame_projector: Callable[[NodeFrame, RuntimeState], dict[str, object]] | None = None,
    ) -> None:
        self.turn_id = turn_id
        self._frame_projector = frame_projector or (lambda frame, _current: frame.to_dict())
        self._lock = RLock()
        self._next_token = 0
        self._subscriptions: dict[int, ActiveTurnSubscription] = {}
        self._latest_nodes: dict[tuple[str, str], RuntimeState] = {}
        self._source_revisions: dict[tuple[str, str], int] = {}
        self._terminal: dict[str, object] | None = None

    def subscribe(self, turn_id: str) -> ActiveTurnSubscription:
        with self._lock:
            token = self._next_token
            self._next_token += 1
            subscription = ActiveTurnSubscription(self, token, turn_id)
            self._subscriptions[token] = subscription
            matching = [(key, node) for key, node in self._latest_nodes.items() if key[1] == turn_id]
            if matching:
                key, node = matching[-1]
                snapshot = NodeFrame.snapshot(node)
                subscription.events.put(self._frame_projector(snapshot, node))
                subscription.source_bases[key] = self._source_revisions[key]
            if self._terminal is not None:
                subscription.events.put({**self._terminal, "terminal_id": turn_id})
            return subscription

    def unsubscribe(self, token: int) -> None:
        with self._lock:
            subscription = self._subscriptions.pop(token, None)
            if subscription is not None:
                subscription.closed = True

    def publish_frame(self, frame: NodeFrame, current: RuntimeState) -> None:
        key = (frame.session_id, frame.turn_id)
        with self._lock:
            self._latest_nodes[key] = current.clone()
            self._source_revisions[key] = frame.revision
            for subscription in self._subscriptions.values():
                if frame.type == "turn.snapshot":
                    snapshot = NodeFrame.snapshot(current)
                    subscription.events.put(self._frame_projector(snapshot, current))
                    subscription.source_bases[key] = frame.revision
                    continue
                base = subscription.source_bases.get(key)
                if base is None:
                    snapshot = NodeFrame.snapshot(current)
                    subscription.events.put(self._frame_projector(snapshot, current))
                    subscription.source_bases[key] = frame.revision
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

    def publish_terminal(self, terminal_type: str, terminal_id: str, message: str = "") -> None:
        terminal = {
            "type": "terminal",
            "terminal_type": terminal_type,
            "terminal_id": terminal_id,
            "message": message,
        }
        with self._lock:
            if self._terminal is not None:
                return
            self._terminal = terminal
            for subscription in self._subscriptions.values():
                subscription.events.put({**terminal, "terminal_id": subscription.expected_turn_id})

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscriptions)


__all__ = ["ActiveTurnStream", "ActiveTurnSubscription"]
