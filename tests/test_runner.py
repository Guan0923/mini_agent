import json
from pathlib import Path

import pytest

from backend.domain import (
    AgentAction,
    AssistantMessage,
)
from backend.observability import JsonlRunLogger
from backend.planning import RuleBasedPlanner
from backend.providers import ModelRequestError
from backend.runtime import LegacyAgentRunner as AgentRunner
from backend.runtime.core.contracts import InterruptDecision
from backend.runtime.planning.review import REQUEST_PLAN_REVIEW_NAME
from backend.tools import ToolRegistry


def test_runner_executes_calculation(tmp_path: Path) -> None:
    events = []
    state = AgentRunner(RuleBasedPlanner(), ToolRegistry(tmp_path)).run(
        "run command python -c 'print((18 + 6) * 4)'", lambda _: True, on_event=events.append
    )

    assert state.status == "completed"
    assert state.final_answer is not None and "96" in state.final_answer


class ProviderFailurePlanner:
    name = "provider-failure"

    def decide(self, history: list[dict[str, str]], mode: str, on_reasoning=None) -> AgentAction:
        raise ModelRequestError(
            "ChatCompletions JSON mode returned empty content.",
            diagnostics={
                "provider": "chat_completions",
                "finish_reason": "stop",
                "content_chars": 0,
                "reasoning_chars": 42,
            },
        )


def test_jsonl_error_includes_provider_diagnostics(tmp_path: Path) -> None:
    log_dir = tmp_path / "logs"
    runner = AgentRunner(ProviderFailurePlanner(), ToolRegistry(tmp_path))
    state = runner.run("produce a plan", on_event=JsonlRunLogger(log_dir))

    records = [
        json.loads(line) for line in (log_dir / f"{state.run_id}.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    error = next(record for record in records if record["kind"] == "error")

    assert state.status == "failed"
    assert error["data"]["provider_diagnostics"] == {
        "provider": "chat_completions",
        "finish_reason": "stop",
        "content_chars": 0,
        "reasoning_chars": 42,
    }


class PlanResearchPlanner:
    name = "plan-research"

    def decide(self, history: list[dict[str, str]], mode: str, on_reasoning=None) -> AgentAction:
        if mode == "plan":
            if history[-1]["content"].startswith("[Tool result]"):
                return AgentAction(
                    type="tool_call",
                    tool=REQUEST_PLAN_REVIEW_NAME,
                    arguments={"plan": "1. Read the note.\n2. Write the reviewed result."},
                )
            return AgentAction(type="tool_call", tool="read_file", arguments={"path": "note.txt"})
        if history[-1]["content"].startswith("[Tool result]"):
            return AgentAction(type="final_answer", answer="Implemented from the reviewed plan.")
        return AgentAction(type="tool_call", tool="write_file", arguments={"path": "result.txt", "content": "done"})


def test_plan_mode_researches_read_only_tools_then_hands_off_to_default_execution(tmp_path: Path) -> None:
    (tmp_path / "note.txt").write_text("reviewed", encoding="utf-8")
    requests = []

    state = AgentRunner(PlanResearchPlanner(), ToolRegistry(tmp_path)).run(
        "review the note",
        mode="plan",
        interrupt=lambda request: (
            requests.append(request.kind) or InterruptDecision("implement" if request.kind == "plan" else "continue")
        ),
    )

    assert state.status == "completed"
    assert state.mode == "agent"
    assert requests == ["plan", "tool"]
    assert (tmp_path / "result.txt").read_text(encoding="utf-8") == "done"
    tool_turn = next(
        message
        for message in reversed(state.history)
        if isinstance(message, AssistantMessage) and message.tool_messages
    )
    assert tool_turn.role == "assistant"
    assert tool_turn.tool_messages[0].role == "tool"
    assert tool_turn.tool_messages[0].status == "succeeded"
    proposals = [
        item
        for item in state.history
        if isinstance(item, AssistantMessage)
        and item.tool_messages
        and item.tool_messages[0].name == REQUEST_PLAN_REVIEW_NAME
    ]
    assert len(proposals) == 1
    assert proposals[0].tool_messages[0].arguments["plan"].startswith("1. Read")


class ConversationPlanner:
    name = "test"

    def decide(self, history: list[dict[str, str]], mode: str, on_reasoning=None) -> AgentAction:
        assert history[-1]["content"] == "How are you?"
        if on_reasoning:
            on_reasoning("A greeting needs no tool.")
        return AgentAction(type="final_answer", answer="I am well.", reasoning="A greeting needs no tool.")


def test_local_read_only_tool_skips_approval(tmp_path: Path) -> None:
    (tmp_path / "note.txt").write_text("read safely", encoding="utf-8")
    events = []
    state = AgentRunner(RuleBasedPlanner(), ToolRegistry(tmp_path)).run(
        "read note.txt",
        lambda _: False,
        on_event=events.append,
        interrupt=lambda _request: InterruptDecision("cancel"),
    )

    assert state.status == "completed"
    assert state.final_answer is not None and "read safely" in state.final_answer
    assert "approval_requested" not in [event.kind for event in events]


class PlanWriteAttemptPlanner:
    name = "plan-write-attempt"

    def decide(self, history: list[dict[str, str]], mode: str, on_reasoning=None) -> AgentAction:
        assert mode == "plan"
        return AgentAction(
            type="tool_call",
            tool="run_command",
            arguments={"command": "[System.IO.File]::WriteAllText('blocked.txt', 'no')"},
        )


def test_plan_mode_blocks_write_tools_during_planning(tmp_path: Path) -> None:
    events = []
    state = AgentRunner(PlanWriteAttemptPlanner(), ToolRegistry(tmp_path)).run(
        "draft a plan",
        mode="plan",
        on_event=events.append,
        interrupt=lambda _request: pytest.fail("Plan mode must not request tool review for a blocked write."),
    )

    assert state.status == "failed"
    assert not (tmp_path / "blocked.txt").exists()
    assert "Execution budget exhausted" in (state.final_answer or "")
    assert "approval_requested" not in [event.kind for event in events]


class OneWriteThenAnswerPlanner:
    name = "one-write-then-answer"

    def decide(self, history: list[dict[str, str]], mode: str, on_reasoning=None) -> AgentAction:
        if history[-1]["content"].startswith("[Tool result]"):
            return AgentAction(type="final_answer", answer="written")
        return AgentAction(
            type="tool_call",
            tool="run_command",
            arguments={"command": "[System.IO.File]::WriteAllText('output.txt', 'done')"},
        )


class RecoveringToolPlanner:
    name = "recovering-tool"

    def __init__(self) -> None:
        self.histories: list[list[dict[str, str]]] = []

    def decide(self, history: list[dict[str, str]], mode: str, on_reasoning=None) -> AgentAction:
        self.histories.append(list(history))
        if history[-1]["content"].startswith("[Tool error]"):
            return AgentAction(type="tool_call", tool="glob", arguments={"pattern": "**/*"})
        if history[-1]["content"].startswith("[Tool result]"):
            return AgentAction(type="final_answer", answer="recovered")
        return AgentAction(type="tool_call", tool="read_file", arguments={"path": "missing.txt"})


def test_tool_errors_feed_back_to_the_planner(tmp_path: Path) -> None:
    planner = RecoveringToolPlanner()
    events = []

    state = AgentRunner(planner, ToolRegistry(tmp_path)).run("recover from a missing file", on_event=events.append)

    assert state.status == "completed"
    assert state.final_answer == "recovered"
    assert "[Tool error]" in planner.histories[1][-1]["content"]
    recoveries = [event for event in state.events if event.kind == "tool_recovery"]
    assert [event.data["attempt"] for event in recoveries] == [1]


class ConsecutiveFailurePlanner:
    name = "consecutive-failure"

    def __init__(self) -> None:
        self.calls = 0

    def decide(self, history: list[dict[str, str]], mode: str, on_reasoning=None) -> AgentAction:
        self.calls += 1
        return AgentAction(type="tool_call", tool="run_command", arguments={"path": f"missing-{self.calls}.txt"})


def test_tool_recovery_continues_until_tool_budget(tmp_path: Path) -> None:
    events = []
    state = AgentRunner(ConsecutiveFailurePlanner(), ToolRegistry(tmp_path), max_tool_calls=3).run(
        "keep failing", on_event=events.append
    )

    assert state.status == "failed"
    assert len(state.actions) == 3
    recoveries = [event for event in state.events if event.kind == "tool_recovery"]
    assert [event.data["attempt"] for event in recoveries] == [1, 2, 3]
    assert not any(event.kind in {"tool_failed", "tool_recovery"} for event in events)


def test_default_runner_confirmation_still_protects_mutating_tools(tmp_path: Path) -> None:
    confirmations = []
    state = AgentRunner(OneWriteThenAnswerPlanner(), ToolRegistry(tmp_path)).run(
        "write a file", confirm=lambda message: confirmations.append(message) or False
    )

    assert state.status == "cancelled"
    assert not (tmp_path / "output.txt").exists()
    assert len(confirmations) == 1


class MemoryCheckpointStore:
    def __init__(self) -> None:
        self.reasons: list[str] = []

    def save(self, state, reason: str) -> None:
        self.reasons.append(reason)


def test_checkpointing_skips_high_volume_reasoning_events(tmp_path: Path) -> None:
    store = MemoryCheckpointStore()
    AgentRunner(ConversationPlanner(), ToolRegistry(tmp_path), checkpoints=store).run("How are you?", lambda _: False)

    assert "thinking_start" not in store.reasons
    assert "thinking_delta" not in store.reasons
    assert "thinking_end" not in store.reasons
    assert store.reasons == ["run_started", "response", "run_finished"]
