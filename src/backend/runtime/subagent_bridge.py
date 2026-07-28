"""Bounded worker-to-parent bridge for subagent events and approvals."""

from __future__ import annotations

from concurrent.futures import Future
from dataclasses import dataclass
from queue import Empty, Full, Queue
from threading import Event, Lock
from typing import Any

from .core.contracts import InterruptDecision, InterruptRequest


@dataclass(frozen=True)
class BridgeEvent:
    kind: str
    message: str
    data: dict[str, Any]


@dataclass(frozen=True)
class BridgeApproval:
    request: InterruptRequest
    response: Future[InterruptDecision]


class ParentRuntimeBridge:
    """Serialize all worker interaction onto the parent invocation thread."""

    def __init__(self, on_event, on_approval, capacity: int = 256) -> None:
        self._on_event = on_event
        self._on_approval = on_approval
        self._queue: Queue[BridgeEvent | BridgeApproval] = Queue(maxsize=capacity)
        self._closed = Event()
        self._put_lock = Lock()

    def event(self, kind: str, message: str, **data: Any) -> None:
        self._put(BridgeEvent(kind, message, data))

    def approval(self, request: InterruptRequest) -> InterruptDecision:
        response: Future[InterruptDecision] = Future()
        self._put(BridgeApproval(request, response))
        return response.result()

    def drain(self) -> int:
        handled = 0
        while True:
            try:
                item = self._queue.get_nowait()
            except Empty:
                return handled
            handled += 1
            if isinstance(item, BridgeEvent):
                self._on_event(item.kind, item.message, **item.data)
                continue
            try:
                item.response.set_result(self._on_approval(item.request))
            except BaseException as exc:
                item.response.set_exception(exc)

    def wait(self, timeout: float) -> None:
        self._closed.wait(timeout)

    def close(self) -> None:
        with self._put_lock:
            self._closed.set()
        while True:
            try:
                item = self._queue.get_nowait()
            except Empty:
                return
            if isinstance(item, BridgeApproval) and not item.response.done():
                item.response.set_exception(RuntimeError("Subagent parent bridge closed."))

    def _put(self, item: BridgeEvent | BridgeApproval) -> None:
        while True:
            with self._put_lock:
                if self._closed.is_set():
                    raise RuntimeError("Subagent parent bridge closed.")
                try:
                    self._queue.put_nowait(item)
                    return
                except Full:
                    pass
            self._closed.wait(0.05)
