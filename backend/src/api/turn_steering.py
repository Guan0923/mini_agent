"""Process-local FIFO steering inboxes for active Web Turns."""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping
from threading import RLock
from typing import Any


class TurnSteeringInbox:
    """Accept steering immediately and expose one entry per safe boundary."""

    def __init__(self) -> None:
        self._items: deque[dict[str, Any]] = deque()
        self._accepted_ids: set[str] = set()
        self._closed = False
        self._lock = RLock()

    def put(self, steering_id: str, message: Mapping[str, Any]) -> bool:
        with self._lock:
            if self._closed:
                return False
            if steering_id in self._accepted_ids:
                return True
            item = dict(message["content"][0])
            self._items.append(
                {
                    "steering_id": steering_id,
                    "content": str(item["text"]),
                    "references": [dict(value) for value in item.get("references", [])],
                }
            )
            self._accepted_ids.add(steering_id)
            return True

    def take(self) -> list[dict[str, Any]]:
        with self._lock:
            return [self._items.popleft()] if self._items else []

    def close(self) -> None:
        with self._lock:
            self._closed = True


__all__ = ["TurnSteeringInbox"]
