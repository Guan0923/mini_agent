"""Execution-budget regression tests."""

from __future__ import annotations

import pytest
from backend.domain import (
    AssistantMessage,
    ExecutionPlan,
    PlanStep,
    RunState,
    StepEvaluation,
    ToolMessage,
)
from backend.planning import LLMPlanner
from backend.runtime import LegacyAgentRunner as AgentRunner
from backend.runtime import PreparedResponse, RunnerSettings, RuntimeState
from backend.tools import Tool, ToolRegistry

from tui import cli


class BatchedPlanner:
    name = "batched"

    def __init__(self, batches: list[int], answer: str = "Project summary") -> None:
        self.batches = batches
        self.answer = answer
        self.decisions = 0
        self.finalizations = 0
        self._next_call = 0

    def decide(self, runtime) -> AssistantMessage:
        del runtime
        index = self.decisions
        self.decisions += 1
        if index >= len(self.batches):
            return AssistantMessage(content=self.answer)
        tools = []
        for _ in range(self.batches[index]):
            self._next_call += 1
            tools.append(ToolMessage(name="inspect", call_id=f"call_{self._next_call}"))
        return AssistantMessage(tool_messages=tools)

    def finalize(self, runtime, reason: str) -> AssistantMessage:
        del runtime, reason
        self.finalizations += 1
        return AssistantMessage(content="Useful budget summary")


class NoFinalizerPlanner:
    name = "no-finalizer"

    def __init__(self) -> None:
        self.calls = 0

    def decide(self, runtime) -> AssistantMessage:
        del runtime
        self.calls += 1
        return AssistantMessage(tool_messages=[ToolMessage(name="inspect", call_id=f"call_{self.calls}")])


class BrokenFinalizerPlanner(NoFinalizerPlanner):
    name = "broken-finalizer"

    def finalize(self, runtime, reason: str) -> AssistantMessage:
        del runtime, reason
        raise RuntimeError("provider unavailable")


class DynamicBudgetPlanner:
    name = "dynamic-budget"

    def __init__(self, step_count: int) -> None:
        self.step_count = step_count
        self.finalizations = 0

    def create_dynamic_plan(self, runtime) -> ExecutionPlan:
        del runtime
        return ExecutionPlan(
            goal="Inspect the project",
            steps=[
                PlanStep(
                    id=f"step_{index}",
                    description=f"Inspect {index}",
                    tool_message=ToolMessage(name="inspect", call_id=f"call_{index}"),
                )
                for index in range(1, self.step_count + 1)
            ],
        )

    def evaluate_step(self, runtime) -> StepEvaluation:
        del runtime
        return StepEvaluation("continue", "The step succeeded.")

    def replan(self, runtime) -> ExecutionPlan:
        raise AssertionError(f"Unexpected replan for {runtime.run.run_id}")

    def finalize(self, runtime, reason: str) -> AssistantMessage:
        del runtime, reason
        self.finalizations += 1
        return AssistantMessage(content="Dynamic budget summary")


def registry(calls: list[str] | None = None) -> ToolRegistry:
    recorded = calls if calls is not None else []
    return ToolRegistry([Tool("inspect", "Inspect", lambda: recorded.append("inspect") or "ok")])


def test_default_budget_allows_one_plus_four_plus_six_plan_reads_then_answer() -> None:
    planner = BatchedPlanner([1, 4, 6])
    state = AgentRunner(planner, registry()).run("Read the project", mode="plan")

    assert state.status == "completed"
    assert state.final_answer == "Project summary"
    assert len(state.actions) == 11
    assert state.model_turns == 4
    assert planner.decisions == 4
    assert planner.finalizations == 0


def test_over_budget_tool_batch_is_rejected_atomically_and_finalized() -> None:
    calls: list[str] = []
    planner = BatchedPlanner([3])
    state = AgentRunner(
        planner,
        registry(calls),
        strategy="reactive",
        max_tool_calls=2,
    ).run("Inspect")

    assert state.status == "failed"
    assert state.final_answer == "Useful budget summary"
    assert state.actions == []
    assert calls == []
    rejected = next(
        message for message in state.history if isinstance(message, AssistantMessage) and message.tool_messages
    )
    assert all(tool.status == "failed" for tool in rejected.tool_messages)
    error = next(event for event in reversed(state.events) if event.kind == "error")
    assert error.data["limit_type"] == "tool_calls"
    assert error.data["limit"] == 2
    assert error.data["tool_calls"] == 0
    assert error.data["finalizer"] == "planner"


def test_model_turn_budget_reserves_one_finalizer_call() -> None:
    planner = BatchedPlanner([1, 1])
    state = AgentRunner(
        planner,
        registry(),
        strategy="reactive",
        max_model_turns=1,
    ).run("Inspect")

    assert state.status == "failed"
    assert state.model_turns == 1
    assert len(state.actions) == 1
    assert planner.decisions == 1
    assert planner.finalizations == 1
    assert state.final_answer == "Useful budget summary"


@pytest.mark.parametrize("planner", [NoFinalizerPlanner(), BrokenFinalizerPlanner()])
def test_budget_finalization_has_a_deterministic_fallback(planner) -> None:
    state = AgentRunner(
        planner,
        registry(),
        strategy="reactive",
        max_model_turns=1,
    ).run("Inspect")

    assert state.status == "failed"
    assert state.final_answer is not None
    assert "Execution budget exhausted" in state.final_answer
    error = next(event for event in reversed(state.events) if event.kind == "error")
    assert error.data["finalizer"] == "fallback"
    if isinstance(planner, BrokenFinalizerPlanner):
        assert error.data["finalization_error"] == "provider unavailable"


def test_dynamic_plan_over_tool_budget_finalizes_without_execution() -> None:
    calls: list[str] = []
    planner = DynamicBudgetPlanner(3)
    state = AgentRunner(
        planner,
        registry(calls),
        strategy="dynamic_replan",
        max_tool_calls=2,
    ).run("Inspect")

    assert state.status == "failed"
    assert state.actions == []
    assert calls == []
    assert planner.finalizations == 1
    assert state.final_answer == "Dynamic budget summary"


def test_dynamic_plan_that_exactly_uses_tool_budget_completes() -> None:
    calls: list[str] = []
    planner = DynamicBudgetPlanner(2)
    state = AgentRunner(
        planner,
        registry(calls),
        strategy="dynamic_replan",
        max_tool_calls=2,
    ).run("Inspect")

    assert state.status == "completed"
    assert len(state.actions) == 2
    assert calls == ["inspect", "inspect"]
    assert planner.finalizations == 0


def test_settings_serialize_new_budgets_and_load_legacy_max_actions() -> None:
    state = RuntimeState(
        session_id="session_budget",
        runner_settings=RunnerSettings(max_model_turns=3, max_tool_calls=7),
    )
    payload = state.to_dict()

    assert payload["runner_settings"]["max_model_turns"] == 3
    assert payload["runner_settings"]["max_tool_calls"] == 7
    assert "max_actions" not in payload["runner_settings"]

    payload["runner_settings"] = {"max_actions": 5}
    restored = RuntimeState.from_dict(payload)
    assert restored.runner_settings.max_model_turns == 8
    assert restored.runner_settings.max_tool_calls == 5
    assert restored.runner_settings.max_actions == 5


def test_run_state_model_turns_round_trip_and_legacy_default() -> None:
    state = RunState(task="Inspect", mode="agent", model_turns=3)
    assert RunState.from_dict(state.to_dict()).model_turns == 3

    payload = state.to_dict()
    payload.pop("model_turns")
    assert RunState.from_dict(payload).model_turns == 0


def test_old_and_new_tool_budget_arguments_conflict() -> None:
    with pytest.raises(ValueError, match="cannot be used together"):
        RunnerSettings(max_actions=4, max_tool_calls=4)
    with pytest.raises(ValueError, match="cannot be used together"):
        AgentRunner(NoFinalizerPlanner(), registry(), max_actions=4, max_tool_calls=4)


def test_llm_finalizer_uses_text_mode_without_tools() -> None:
    class CaptureClient:
        def __init__(self) -> None:
            self.request = None

        def run(self, runtime):
            self.request = (
                runtime.exchange.operation,
                runtime.exchange.output_mode,
                list(runtime.exchange.allowed_tools),
            )
            return PreparedResponse(AssistantMessage(content="Bounded summary"))

    client = CaptureClient()
    planner = LLMPlanner(client, ["inspect"], ["inspect"])
    runner = AgentRunner(NoFinalizerPlanner(), registry(), strategy="reactive")
    runtime = runner.new_runtime(task="Inspect")

    message = planner.finalize(runtime, "tool budget exhausted")

    assert message.content == "Bounded summary"
    assert client.request == ("finalize", "text", [])


def test_cli_rejects_old_and_new_tool_budget_flags_together(tmp_path, capsys) -> None:
    with pytest.raises(SystemExit) as exc_info:
        cli.main(
            [
                "--workspace",
                str(tmp_path),
                "--planner",
                "rule",
                "--max-actions",
                "4",
                "--max-tool-calls",
                "4",
            ]
        )

    assert exc_info.value.code == 2
    assert "cannot be used together" in capsys.readouterr().err
