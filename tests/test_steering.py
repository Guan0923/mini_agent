from __future__ import annotations

from collections import deque
from pathlib import Path

from backend.domain import AssistantMessage, ToolMessage, UserMessage
from backend.runtime import AgentRunner, ConversationService, PostgresSessionStore
from backend.tools import Tool, ToolRegistry


class SteeringPlanner:
    name = "steering"

    def __init__(self) -> None:
        self.calls = 0

    def decide(self, runtime):
        self.calls += 1
        if self.calls == 1:
            return AssistantMessage(tool_messages=[ToolMessage(name="work", call_id="call_1")])
        latest = next(
            message.content for message in reversed(runtime.state.messages) if isinstance(message, UserMessage)
        )
        return AssistantMessage(content=f"adjusted: {latest}")


def sequence_handler(values: list[list[str]]):
    pending = deque(values)

    def drain() -> list[str]:
        return pending.popleft() if pending else []

    return drain


def test_steering_after_model_response_discards_stale_tool_call() -> None:
    calls: list[str] = []
    runner = AgentRunner(
        SteeringPlanner(),
        ToolRegistry([Tool("work", "Work", lambda: calls.append("called") or "done")]),
        strategy="reactive",
    )
    runtime = runner.new_runtime(task="start")
    runtime.services.steering = sequence_handler([[], ["change direction"], [], []])

    result = runner.run(runtime)

    assert result.status == "completed"
    assert result.final_answer == "adjusted: change direction"
    assert calls == []
    assert any(event.kind == "steering_applied" for event in result.events)


def test_cancellation_after_model_response_discards_stale_tool_call() -> None:
    calls: list[str] = []
    cancel_requested = False

    class CancellingPlanner:
        name = "cancelling"

        def decide(self, runtime):
            nonlocal cancel_requested
            cancel_requested = True
            return AssistantMessage(tool_messages=[ToolMessage(name="work", call_id="call_1")])

    runner = AgentRunner(
        CancellingPlanner(),
        ToolRegistry([Tool("work", "Work", lambda: calls.append("called") or "done")]),
        strategy="reactive",
    )
    runtime = runner.new_runtime(task="start")
    runtime.services.cancel_requested = lambda: cancel_requested

    result = runner.run(runtime)

    assert result.status == "cancelled"
    assert calls == []
    assert [event.kind for event in result.events[-3:]] == ["cancelled", "tool_failed", "run_finished"]


def test_cancellation_during_tool_keeps_result_and_skips_remaining_tools() -> None:
    calls: list[str] = []
    cancel_requested = False

    class TwoToolPlanner:
        name = "two-tool-cancellation"

        def decide(self, runtime):
            return AssistantMessage(
                tool_messages=[
                    ToolMessage(name="first", call_id="call_1"),
                    ToolMessage(name="second", call_id="call_2"),
                ]
            )

    def first() -> str:
        nonlocal cancel_requested
        calls.append("first")
        cancel_requested = True
        return "first result"

    runner = AgentRunner(
        TwoToolPlanner(),
        ToolRegistry(
            [
                Tool("first", "First", first),
                Tool("second", "Second", lambda: calls.append("second") or "second result"),
            ]
        ),
        strategy="reactive",
    )
    runtime = runner.new_runtime(task="start")
    runtime.services.cancel_requested = lambda: cancel_requested

    result = runner.run(runtime)

    assert result.status == "cancelled"
    assert calls == ["first"]
    tool_turn = next(
        message for message in runtime.state.messages if isinstance(message, AssistantMessage) and message.tool_messages
    )
    assert [tool.status for tool in tool_turn.tool_messages] == ["succeeded", "failed"]
    assert tool_turn.tool_messages[1].content == "Not executed because the run was cancelled."


def test_conversation_persists_cooperatively_cancelled_run(tmp_path: Path) -> None:
    store = PostgresSessionStore()
    cancel_requested = False

    class CancellingPlanner:
        name = "persisted-cancellation"

        def decide(self, runtime):
            nonlocal cancel_requested
            cancel_requested = True
            return AssistantMessage(content="stale response")

    service = ConversationService(
        AgentRunner(CancellingPlanner(), ToolRegistry([]), strategy="reactive"),
        store,
    )

    result = service.run_task("start", mode="agent", cancel_requested=lambda: cancel_requested)

    assert result.status == "cancelled"
    assert service.active_session is not None
    restored = store.load_runtime(service.active_session.session_id)
    assert restored is not None
    assert restored.status == "idle"
    assert restored.current_run is not None
    assert restored.current_run.status == "cancelled"


def test_steering_during_tool_keeps_result_and_stops_remaining_actions() -> None:
    queued: list[str] = []
    calls: list[str] = []

    class TwoToolPlanner(SteeringPlanner):
        def decide(self, runtime):
            self.calls += 1
            if self.calls == 1:
                return AssistantMessage(
                    tool_messages=[
                        ToolMessage(name="first", call_id="call_1"),
                        ToolMessage(name="second", call_id="call_2"),
                    ]
                )
            return AssistantMessage(content="replanned")

    def first() -> str:
        calls.append("first")
        queued.append("use the first result only")
        return "first result"

    def drain() -> list[str]:
        messages = list(queued)
        queued.clear()
        return messages

    runner = AgentRunner(
        TwoToolPlanner(),
        ToolRegistry(
            [
                Tool("first", "First", first),
                Tool("second", "Second", lambda: calls.append("second") or "second result"),
            ]
        ),
        strategy="reactive",
    )
    runtime = runner.new_runtime(task="start")
    runtime.services.steering = drain

    result = runner.run(runtime)

    assert result.status == "completed"
    assert calls == ["first"]
    tool_turn = next(
        message for message in runtime.state.messages if isinstance(message, AssistantMessage) and message.tool_messages
    )
    assert [tool.status for tool in tool_turn.tool_messages] == ["succeeded", "failed"]
    steering_index = next(
        index
        for index, message in enumerate(runtime.state.messages)
        if isinstance(message, UserMessage) and message.content == "use the first result only"
    )
    assert runtime.state.messages.index(tool_turn) < steering_index


def test_conversation_persists_merged_in_run_messages(tmp_path: Path) -> None:
    store = PostgresSessionStore()
    service = ConversationService(
        AgentRunner(SteeringPlanner(), ToolRegistry([Tool("work", "Work", lambda: "done")]), strategy="reactive"),
        store,
    )
    handler = sequence_handler([[], ["first update", "second update"], [], []])

    result = service.run_task("start", mode="agent", steering=handler)

    assert result.status == "completed"
    assert service.active_session is not None
    assert store.load_conversation(service.active_session.session_id) == [
        {"role": "user", "content": "start"},
        {"role": "user", "content": "first update\n\nsecond update"},
        {"role": "assistant", "content": "adjusted: first update\n\nsecond update"},
    ]
    restored = store.load_runtime(service.active_session.session_id)
    assert restored is not None
    assert [message.content for message in restored.messages if isinstance(message, UserMessage)] == [
        "start",
        "first update\n\nsecond update",
    ]
