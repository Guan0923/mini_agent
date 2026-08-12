import json
from pathlib import Path

import pytest
from backend.domain import (
    AgentAction,
    AssistantMessage,
    ExecutionPlan,
    PlanStep,
    StepEvaluation,
    StrategySelection,
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
            "DeepSeek JSON mode returned empty content.",
            diagnostics={
                "provider": "deepseek",
                "finish_reason": "stop",
                "content_chars": 0,
                "reasoning_chars": 42,
            },
        )


def test_jsonl_error_includes_provider_diagnostics(tmp_path: Path) -> None:
    log_dir = tmp_path / "logs"
    runner = AgentRunner(ProviderFailurePlanner(), ToolRegistry(tmp_path), strategy="reactive")
    state = runner.run("produce a plan", on_event=JsonlRunLogger(log_dir))

    records = [
        json.loads(line) for line in (log_dir / f"{state.run_id}.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    error = next(record for record in records if record["kind"] == "error")

    assert state.status == "failed"
    assert error["data"]["provider_diagnostics"] == {
        "provider": "deepseek",
        "finish_reason": "stop",
        "content_chars": 0,
        "reasoning_chars": 42,
    }


class PlanResearchPlanner:
    name = "plan-research"

    def decide(self, history: list[dict[str, str]], mode: str, on_reasoning=None) -> AgentAction:
        assert mode == "plan"
        if history[-1]["content"].startswith("[Tool result]"):
            return AgentAction(
                type="tool_call",
                tool=REQUEST_PLAN_REVIEW_NAME,
                arguments={"plan": "1. Read the note.\n2. Write the reviewed result."},
            )
        return AgentAction(type="tool_call", tool="read_file", arguments={"path": "note.txt"})

    def select_strategy(self, runtime) -> StrategySelection:
        return StrategySelection("dynamic_replan", "The reviewed plan needs staged execution.")

    def create_dynamic_plan(self, history: list[dict[str, str]], mode: str, on_reasoning=None) -> ExecutionPlan:
        assert mode == "agent"
        assert history[-1] == {"role": "user", "content": "Implement the plan"}
        assert any(item["role"] == "assistant" and REQUEST_PLAN_REVIEW_NAME in item["content"] for item in history)
        return ExecutionPlan(
            goal="Write the reviewed result.",
            steps=[
                PlanStep(
                    id="write",
                    description="Write the reviewed result",
                    action=AgentAction(
                        type="tool_call", tool="write_file", arguments={"path": "result.txt", "content": "done"}
                    ),
                )
            ],
        )

    def evaluate_step(self, history, plan, step, result) -> StepEvaluation:
        return StepEvaluation("continue", "The write completed.")

    def replan(self, history, plan, reason, on_reasoning=None) -> ExecutionPlan:
        raise AssertionError("The test plan should not need replanning.")


def test_plan_mode_researches_read_only_tools_then_hands_off_to_dynamic_execution(tmp_path: Path) -> None:
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
    assert state.strategy == "dynamic_replan"
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

    def select_strategy(self, history: list[dict[str, str]], mode: str) -> StrategySelection:
        return StrategySelection("reactive", "A greeting needs no precomputed plan.")


class DynamicRecoveryPlanner:
    name = "dynamic-recovery"

    def decide(self, history: list[dict[str, str]], mode: str, on_reasoning=None) -> AgentAction:
        raise AssertionError("dynamic_replan executes its generated plan")

    def select_strategy(self, history: list[dict[str, str]], mode: str) -> StrategySelection:
        return StrategySelection("dynamic_replan", "The first tool may fail and requires a fallback.")

    def create_plan(self, history: list[dict[str, str]], mode: str, on_reasoning=None) -> ExecutionPlan:
        return ExecutionPlan(
            goal="Create a result file even if the preferred source is absent.",
            steps=[
                PlanStep(
                    id="missing-source",
                    description="Read the preferred source file",
                    action=AgentAction(
                        type="tool_call", tool="run_command", arguments={"command": "Get-Content missing.txt"}
                    ),
                ),
                PlanStep(
                    id="old-write",
                    description="Write the original result",
                    action=AgentAction(
                        type="tool_call",
                        tool="run_command",
                        arguments={"command": "[System.IO.File]::WriteAllText('result.txt', '4')"},
                    ),
                ),
            ],
        )

    def evaluate_step(self, history, plan, step, result) -> StepEvaluation:
        return StepEvaluation("continue", "The successful step did not invalidate the plan.")

    def replan(self, history, plan, reason, on_reasoning=None) -> ExecutionPlan:
        assert plan.steps[0].status == "failed"
        assert "missing" in reason
        assert "[Tool call] run_command" in history[-2]["content"]
        assert "[Tool error]" in history[-1]["content"]
        return ExecutionPlan(
            goal="Use the fallback result.",
            steps=[
                PlanStep(
                    id="fallback-write",
                    description="Write a fallback result",
                    action=AgentAction(
                        type="tool_call",
                        tool="run_command",
                        arguments={"command": "[System.IO.File]::WriteAllText('fallback.txt', 'fallback')"},
                    ),
                )
            ],
        )


def test_dynamic_replan_replaces_unfinished_steps_after_tool_failure(tmp_path: Path) -> None:
    events = []
    state = AgentRunner(DynamicRecoveryPlanner(), ToolRegistry(tmp_path)).run(
        "recover from a missing source", lambda _: True, on_event=events.append
    )

    assert state.status == "completed"
    assert state.strategy == "dynamic_replan"
    assert state.replan_count == 1
    assert len(state.plan_history) == 1
    assert [step.status for step in state.plan_history[0].steps] == ["failed", "superseded"]
    assert state.plan is not None
    assert state.plan.revision == 2
    assert state.plan.steps[0].status == "completed"
    assert (tmp_path / "fallback.txt").read_text(encoding="utf-8") == "fallback"
    assert not (tmp_path / "old.txt").exists()
    assert "replan_requested" in [event.kind for event in state.events]
    assert "replan_applied" in [event.kind for event in events]


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

    def select_strategy(self, history: list[dict[str, str]], mode: str) -> StrategySelection:
        return StrategySelection("reactive", "The tool error can be corrected with another action.")


def test_reactive_workflow_feeds_tool_errors_back_to_the_planner(tmp_path: Path) -> None:
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

    def select_strategy(self, history: list[dict[str, str]], mode: str) -> StrategySelection:
        return StrategySelection("reactive", "The test exercises the recovery budget.")


def test_reactive_tool_recovery_continues_until_tool_budget(tmp_path: Path) -> None:
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
    state = AgentRunner(OneWriteThenAnswerPlanner(), ToolRegistry(tmp_path), strategy="reactive").run(
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
    assert store.reasons == ["run_started", "strategy", "response", "run_finished"]
