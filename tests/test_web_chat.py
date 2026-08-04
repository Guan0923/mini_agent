"""Regression tests for HTTP/SSE chat cancellation and interactive cleanup."""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path
from types import SimpleNamespace

from backend.api import chat
from backend.api.interrupts import make_interactive_interrupt, registry
from backend.runtime.core.contracts import InterruptRequest
from backend.runtime.core.events import RuntimeEvent


class _FakeApp:
    def __init__(self, conversation: object) -> None:
        self.conversation = conversation
        self.closed = False

    def open_conversation(self) -> object:
        return self.conversation

    def close(self) -> None:
        self.closed = True


class _BlockingConversation:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.released = threading.Event()
        self.cancel_requested = None

    def run_task(self, _prompt: str, **kwargs: object) -> SimpleNamespace:
        self.cancel_requested = kwargs["cancel_requested"]
        sink = kwargs["on_event"]
        assert callable(sink)
        sink(RuntimeEvent("thinking_start", "thinking"))
        self.started.set()
        while not self.cancel_requested():  # type: ignore[operator]
            time.sleep(0.005)
        self.released.set()
        return SimpleNamespace(status="cancelled", final_answer="", run_id="run-cancelled")


def test_closing_sse_stream_requests_runtime_cancellation(monkeypatch, tmp_path: Path) -> None:
    conversation = _BlockingConversation()
    app = _FakeApp(conversation)
    monkeypatch.setattr(chat, "build_application", lambda *_args, **_kwargs: app)
    state = SimpleNamespace(chat_workspace=tmp_path)

    async def scenario() -> None:
        stream = chat._stream(state, "stop me", False)
        first = await stream.__anext__()
        assert '"kind": "thinking_start"' in first
        assert conversation.started.wait(timeout=1)
        await stream.aclose()

    asyncio.run(scenario())
    assert conversation.released.wait(timeout=1)
    assert app.closed is True


def test_interactive_interrupt_wakes_when_connection_is_cancelled() -> None:
    cancelled = threading.Event()
    messages: list[dict] = []
    interrupt = make_interactive_interrupt(messages.append, timeout=10, cancel_requested=cancelled.is_set)
    request = InterruptRequest("tool", "Approve this", {"tool": "write_file"})
    result: list[object] = []

    worker = threading.Thread(target=lambda: result.append(interrupt(request)), daemon=True)
    worker.start()
    deadline = time.monotonic() + 1
    while not messages and time.monotonic() < deadline:
        time.sleep(0.005)
    cancelled.set()
    worker.join(timeout=1)

    assert not worker.is_alive()
    assert result and getattr(result[0], "choice") == "cancel"
    decision_id = messages[0]["data"]["decision_id"]
    assert registry.resolve(decision_id, {"choice": "continue"}) is False
