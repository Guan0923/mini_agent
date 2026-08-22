"""Regression tests for HTTP/SSE chat cancellation and interactive cleanup."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.api import chat
from backend.api.interrupts import make_interactive_interrupt, registry
from backend.domain.runtime_state import InMemoryNodeStore
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


class _CompletingConversation:
    def run_task(self, _prompt: str, **kwargs: object) -> SimpleNamespace:
        sink = kwargs["on_event"]
        assert callable(sink)
        sink(
            RuntimeEvent(
                "run_finished",
                "completed",
                {"final_answer": "finished answer", "duration_ms": 12.0, "model_calls": 1, "tool_calls": 0},
            )
        )
        return SimpleNamespace(status="completed", final_answer="finished answer", run_id="run-completed")


class _TerminalConversation:
    def __init__(self, outcome: list[RuntimeEvent] | BaseException) -> None:
        self.active_session = SimpleNamespace(session_id="session-terminal")
        self.outcome = outcome
        self.runtime_node_bridge = None

    def ensure_session(self, _prompt: str | None = None) -> None:
        return None

    def attach_runtime_node_bridge(self, bridge, *, events_external: bool = True) -> None:
        # Mirror ConversationService._bind_node_bridge: the attached bridge is
        # started before the run so streamed events reach a live sidecar.
        self.runtime_node_bridge = bridge
        bridge.start()

    def run_task(self, _prompt: str, **kwargs: object) -> SimpleNamespace:
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        sink = kwargs["on_event"]
        assert callable(sink)
        for event in self.outcome:
            sink(event)
        return SimpleNamespace(status="failed", final_answer="", run_id="run-terminal")


class _NodeApp(_FakeApp):
    def __init__(self, conversation: _TerminalConversation) -> None:
        super().__init__(conversation)
        self.session_store = InMemoryNodeStore()

    def open_conversation(self, *_args: object) -> object:
        return self.conversation


class _CancelledConversation(_TerminalConversation):
    def run_task(self, _prompt: str, **kwargs: object) -> SimpleNamespace:
        sink = kwargs["on_event"]
        assert callable(sink)
        sink(RuntimeEvent("response_delta", "partial"))
        sink(RuntimeEvent("cancelled", "Run cancelled by user", {"stop_reason": "user_cancelled"}))
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


def test_completed_sse_stream_delivers_done_payload(monkeypatch, tmp_path: Path) -> None:
    app = _FakeApp(_CompletingConversation())
    monkeypatch.setattr(chat, "build_application", lambda *_args, **_kwargs: app)
    state = SimpleNamespace(chat_workspace=tmp_path)

    async def scenario() -> list[str]:
        stream = chat._stream(state, "finish me", False)
        return [item async for item in stream]

    items = asyncio.run(scenario())
    assert any('"kind": "run_finished"' in item for item in items)
    assert any('"type": "done"' in item and "finished answer" in item for item in items)
    assert app.closed is True


def test_cancelled_sse_stream_delivers_cancel_without_error_payload(monkeypatch, tmp_path: Path) -> None:
    app = _NodeApp(_CancelledConversation([]))
    monkeypatch.setattr(chat, "build_application", lambda *_args, **_kwargs: app)
    state = SimpleNamespace(chat_workspace=tmp_path)

    async def scenario() -> list[dict[str, object]]:
        stream = chat._stream(state, "pause me", False)
        items = [item async for item in stream]
        return [json.loads(item.removeprefix("data: ").strip()) for item in items]

    items = asyncio.run(scenario())
    terminal = next(item for item in items if item.get("status") == "cancel")
    assert terminal["type"] == "done"
    assert "error" not in terminal
    assert any(
        item.get("type") == "node.delete"
        and isinstance(item.get("node"), dict)
        and item["node"].get("status") == "cancel"
        for item in items
    )
    assert app.closed is True


@pytest.mark.parametrize(
    ("outcome", "status", "expected"),
    [
        (
            [
                RuntimeEvent("tool_failed", "disk unavailable", {"tool": "write", "call_id": "c1"}),
                RuntimeEvent("error", "Stopped"),
            ],
            "abort",
            "internal tool error",
        ),
        (RuntimeError("unexpected failure"), "abort", "agent encountered an internal error"),
    ],
)
def test_terminal_sse_payload_explains_failed_and_abort_reasons(
    monkeypatch, tmp_path: Path, outcome: list[RuntimeEvent] | BaseException, status: str, expected: str
) -> None:
    app = _NodeApp(_TerminalConversation(outcome))
    monkeypatch.setattr(chat, "build_application", lambda *_args, **_kwargs: app)
    state = SimpleNamespace(chat_workspace=tmp_path)

    async def scenario() -> list[dict[str, object]]:
        stream = chat._stream(state, "terminal", False)
        items = [item async for item in stream]
        return [json.loads(item.removeprefix("data: ").strip()) for item in items]

    items = asyncio.run(scenario())
    terminal = next(item for item in items if item.get("type") == "error")
    assert terminal["status"] == status
    assert expected in str(terminal["error"])
    assert app.closed is True
