from pathlib import Path

from mini_agent.domain import AgentAction, ExecutionPlan, PlanStep, StepEvaluation, StrategySelection
from mini_agent.planning import RuleBasedPlanner
import pytest

from mini_agent.runtime import AgentRunner, RunnerSettings, SQLiteCheckpointStore
from mini_agent.runtime.contracts import InterruptDecision, InterruptRequest
from mini_agent.tools import ToolRegistry


def test_runner_executes_calculation(tmp_path: Path) -> None:
    events = []
    state = AgentRunner(RuleBasedPlanner(), ToolRegistry(tmp_path)).run(
        "calculate (18 + 6) * 4", lambda _: False, on_event=events.append
    )

    assert state.status == "completed"
    assert state.final_answer == "96"
    assert state.completed_steps == [1]
    assert any(event.kind == "tool_result" and event.message == "96" for event in events)


class PlanModePlanner:
    name = "test"

    def create_plan(self, history: list[dict[str, str]], mode: str, on_reasoning=None) -> ExecutionPlan:
        return ExecutionPlan(
            goal="Write a file after approval.",
            steps=[
                PlanStep(
                    id="write",
                    description="Write the approved file",
                    action=AgentAction(type="tool_call", tool="write_file", arguments={"path": "blocked.txt", "content": "no"}),
                )
            ],
        )


def test_plan_mode_cancel_prevents_execution(tmp_path: Path) -> None:
    events = []
    state = AgentRunner(PlanModePlanner(), ToolRegistry(tmp_path)).run(
        "write a file",
        lambda _: True,
        mode="plan",
        on_event=events.append,
        interrupt=lambda _request: InterruptDecision("cancel"),
    )

    assert state.status == "cancelled"
    assert not (tmp_path / "blocked.txt").exists()
    assert [event.kind for event in events[-2:]] == ["cancelled", "run_finished"]


class ConversationPlanner:
    name = "test"

    def decide(self, history: list[dict[str, str]], mode: str, on_reasoning=None) -> AgentAction:
        assert history[-1]["content"] == "How are you?"
        if on_reasoning:
            on_reasoning("A greeting needs no tool.")
        return AgentAction(type="final_answer", answer="I am well.", reasoning="A greeting needs no tool.")

    def select_strategy(self, history: list[dict[str, str]], mode: str) -> StrategySelection:
        return StrategySelection("reactive", "A greeting needs no precomputed plan.")


def test_agent_mode_emits_reasoning_and_persists_conversation(tmp_path: Path) -> None:
    events = []
    conversation = [{"role": "user", "content": "Hello"}, {"role": "assistant", "content": "Hi"}]
    state = AgentRunner(ConversationPlanner(), ToolRegistry(tmp_path)).run(
        "How are you?", lambda _: False, conversation=conversation, on_event=events.append
    )

    assert state.status == "completed"
    assert [event.kind for event in events] == [
        "run_started",
        "strategy",
        "thinking_start",
        "thinking_delta",
        "thinking_end",
        "response",
        "run_finished",
    ]
    assert all(event.data["run_id"] == state.run_id for event in events)
    assert conversation[-2:] == [{"role": "user", "content": "How are you?"}, {"role": "assistant", "content": "I am well."}]


class FixedPlanPlanner:
    name = "fixed-plan"

    def decide(self, history: list[dict[str, str]], mode: str, on_reasoning=None) -> AgentAction:
        raise AssertionError("plan_execute should not call decide after the plan is created")

    def select_strategy(self, history: list[dict[str, str]], mode: str) -> StrategySelection:
        return StrategySelection("plan_execute", "The test task has a fixed multi-step workflow.")

    def create_plan(self, history: list[dict[str, str]], mode: str, on_reasoning=None) -> ExecutionPlan:
        assert history[-1]["content"] == "calculate then write"
        return ExecutionPlan(
            goal="Calculate a value and write it to a file.",
            steps=[
                PlanStep(
                    id="calculate",
                    description="Calculate 2 + 2",
                    action=AgentAction(type="tool_call", tool="calculator", arguments={"expression": "2 + 2"}),
                ),
                PlanStep(
                    id="write",
                    description="Write the fixed result",
                    action=AgentAction(
                        type="tool_call", tool="write_file", arguments={"path": "result.txt", "content": "4"}
                    ),
                ),
            ],
        )


def test_plan_execute_persists_and_executes_a_fixed_plan(tmp_path: Path) -> None:
    events = []
    state = AgentRunner(FixedPlanPlanner(), ToolRegistry(tmp_path)).run(
        "calculate then write", lambda _: True, on_event=events.append
    )

    assert state.status == "completed"
    assert state.strategy == "plan_execute"
    assert state.plan is not None
    assert [step.status for step in state.plan.steps] == ["completed", "completed"]
    assert state.completed_steps == [1, 2]
    assert (tmp_path / "result.txt").read_text(encoding="utf-8") == "4"
    assert [event.kind for event in events[:3]] == ["run_started", "strategy", "plan"]
    assert state.final_answer is not None and "Execution plan completed" in state.final_answer


class FailingPlanPlanner(FixedPlanPlanner):
    name = "failing-plan"

    def create_plan(self, history: list[dict[str, str]], mode: str, on_reasoning=None) -> ExecutionPlan:
        return ExecutionPlan(
            goal="Demonstrate a failed plan.",
            steps=[
                PlanStep(
                    id="missing",
                    description="Read a missing file",
                    action=AgentAction(type="tool_call", tool="read_file", arguments={"path": "missing.txt"}),
                ),
                PlanStep(
                    id="must-not-run",
                    description="Write a file",
                    action=AgentAction(
                        type="tool_call", tool="write_file", arguments={"path": "must-not-run.txt", "content": "no"}
                    ),
                ),
            ],
        )


def test_plan_execute_stops_after_a_failed_step(tmp_path: Path) -> None:
    state = AgentRunner(FailingPlanPlanner(), ToolRegistry(tmp_path), strategy="plan_execute").run(
        "run a failing plan", lambda _: True
    )

    assert state.status == "failed"
    assert state.plan is not None
    assert [step.status for step in state.plan.steps] == ["failed", "pending"]
    assert not (tmp_path / "must-not-run.txt").exists()


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
                    action=AgentAction(type="tool_call", tool="read_file", arguments={"path": "missing.txt"}),
                ),
                PlanStep(
                    id="old-write",
                    description="Write the original result",
                    action=AgentAction(
                        type="tool_call", tool="write_file", arguments={"path": "old.txt", "content": "old"}
                    ),
                ),
            ],
        )

    def evaluate_step(self, history, plan, step, result) -> StepEvaluation:
        return StepEvaluation("continue", "The successful step did not invalidate the plan.")

    def replan(self, history, plan, reason, on_reasoning=None) -> ExecutionPlan:
        assert plan.steps[0].status == "failed"
        assert "missing" in reason
        return ExecutionPlan(
            goal="Use the fallback result.",
            steps=[
                PlanStep(
                    id="fallback-write",
                    description="Write a fallback result",
                    action=AgentAction(
                        type="tool_call", tool="write_file", arguments={"path": "fallback.txt", "content": "fallback"}
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
    assert "replan_requested" in [event.kind for event in events]
    assert "replan_applied" in [event.kind for event in events]


class DeviatingPlanPlanner(DynamicRecoveryPlanner):
    name = "deviating-plan"

    def create_plan(self, history: list[dict[str, str]], mode: str, on_reasoning=None) -> ExecutionPlan:
        return ExecutionPlan(
            goal="Write a verified value.",
            steps=[
                PlanStep(
                    id="calculate",
                    description="Calculate a value",
                    action=AgentAction(type="tool_call", tool="calculator", arguments={"expression": "2 + 2"}),
                ),
                PlanStep(
                    id="old-write",
                    description="Write the old result",
                    action=AgentAction(
                        type="tool_call", tool="write_file", arguments={"path": "old.txt", "content": "old"}
                    ),
                ),
            ],
        )

    def evaluate_step(self, history, plan, step, result) -> StepEvaluation:
        if step.id == "calculate":
            return StepEvaluation("replan", "The calculation result requires a different output file.")
        return StepEvaluation("continue", "The replacement step completed the goal.")

    def replan(self, history, plan, reason, on_reasoning=None) -> ExecutionPlan:
        return ExecutionPlan(
            goal="Write the verified value.",
            steps=[
                PlanStep(
                    id="verified-write",
                    description="Write the verified result",
                    action=AgentAction(
                        type="tool_call", tool="write_file", arguments={"path": "verified.txt", "content": "4"}
                    ),
                )
            ],
        )


def test_dynamic_replan_replaces_remaining_steps_after_result_deviation(tmp_path: Path) -> None:
    state = AgentRunner(DeviatingPlanPlanner(), ToolRegistry(tmp_path)).run("handle a deviation", lambda _: True)

    assert state.status == "completed"
    assert state.replan_count == 1
    assert state.plan_history[0].steps[0].status == "completed"
    assert state.plan_history[0].steps[1].status == "superseded"
    assert (tmp_path / "verified.txt").read_text(encoding="utf-8") == "4"


def test_dynamic_replan_stops_when_replan_budget_is_exhausted(tmp_path: Path) -> None:
    state = AgentRunner(
        DynamicRecoveryPlanner(), ToolRegistry(tmp_path), strategy="dynamic_replan", max_replans=0
    ).run("recover from a missing source", lambda _: True)

    assert state.status == "failed"
    assert state.replan_count == 0
    assert state.final_answer is not None and "Stopped after 0 replans" in state.final_answer


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"max_actions": 0}, "max_actions"),
        ({"max_retries": -1}, "max_retries"),
        ({"max_replans": -1}, "max_replans"),
        ({"strategy": "unknown"}, "strategy"),
    ],
)
def test_runner_settings_validate_limits(kwargs: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        RunnerSettings(**kwargs)


def test_tool_interrupt_cancel_prevents_call(tmp_path: Path) -> None:
    events = []
    state = AgentRunner(RuleBasedPlanner(), ToolRegistry(tmp_path)).run(
        "calculate 2 + 2",
        lambda _: False,
        on_event=events.append,
        interrupt=lambda _request: InterruptDecision("cancel"),
    )

    assert state.status == "cancelled"
    assert "tool_result" not in [event.kind for event in events]
    assert "approval_requested" in [event.kind for event in events]


class OneWriteThenAnswerPlanner:
    name = "one-write-then-answer"

    def decide(self, history: list[dict[str, str]], mode: str, on_reasoning=None) -> AgentAction:
        if history[-1]["content"].startswith("[Tool result]"):
            return AgentAction(type="final_answer", answer="written")
        return AgentAction(type="tool_call", tool="write_file", arguments={"path": "approved.txt", "content": "ok"})


def test_human_interrupt_continue_is_the_only_runtime_confirmation(tmp_path: Path) -> None:
    confirmations = []
    state = AgentRunner(OneWriteThenAnswerPlanner(), ToolRegistry(tmp_path), strategy="reactive").run(
        "write a file",
        confirm=lambda message: confirmations.append(message) or False,
        interrupt=lambda _request: InterruptDecision("continue"),
    )

    assert state.status == "completed"
    assert (tmp_path / "approved.txt").read_text(encoding="utf-8") == "ok"
    assert confirmations == []


def test_default_runner_confirmation_still_protects_mutating_tools(tmp_path: Path) -> None:
    confirmations = []
    state = AgentRunner(OneWriteThenAnswerPlanner(), ToolRegistry(tmp_path), strategy="reactive").run(
        "write a file", confirm=lambda message: confirmations.append(message) or False
    )

    assert state.status == "cancelled"
    assert not (tmp_path / "approved.txt").exists()
    assert len(confirmations) == 1


class FeedbackPlanPlanner:
    name = "feedback-plan"

    def create_plan(self, history: list[dict[str, str]], mode: str, on_reasoning=None) -> ExecutionPlan:
        revised = history[-1]["content"].startswith("[Plan feedback]")
        path = "revised.txt" if revised else "original.txt"
        return ExecutionPlan(
            goal="Write the requested file.",
            steps=[
                PlanStep(
                    id="write",
                    description=f"Write {path}",
                    action=AgentAction(type="tool_call", tool="write_file", arguments={"path": path, "content": "ok"}),
                )
            ],
        )


def test_plan_mode_supplement_revises_plan_and_checkpoints_approval(tmp_path: Path) -> None:
    store = SQLiteCheckpointStore(tmp_path / "checkpoints.db")
    decisions = iter([InterruptDecision("supplement", "write a revised file"), InterruptDecision("continue"), InterruptDecision("continue")])
    observed_checkpoint = []

    def interrupt(request: InterruptRequest) -> InterruptDecision:
        saved = store.load(request.data["run_id"])
        assert saved is not None
        assert saved.events[-1].kind == "approval_requested"
        observed_checkpoint.append(request.kind)
        return next(decisions)

    runner = AgentRunner(FeedbackPlanPlanner(), ToolRegistry(tmp_path), checkpoints=store)
    state = runner.run("write a file", lambda _: True, mode="plan", interrupt=interrupt)

    assert state.status == "completed"
    assert state.mode == "agent"
    assert state.plan is not None and state.plan.revision == 2
    assert (tmp_path / "revised.txt").exists()
    assert not (tmp_path / "original.txt").exists()
    assert observed_checkpoint == ["plan", "plan", "tool"]
    assert state.history[-1]["content"] == "[Plan feedback]\nwrite a revised file"
    saved = store.load(state.run_id)
    assert saved is not None and saved.status == "completed"


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


class FeedbackReplanner:
    name = "feedback-replanner"

    def decide(self, history: list[dict[str, str]], mode: str, on_reasoning=None) -> AgentAction:
        raise AssertionError("The fixed plan should be used.")

    def create_plan(self, history: list[dict[str, str]], mode: str, on_reasoning=None) -> ExecutionPlan:
        return ExecutionPlan(
            goal="Run two calculations.",
            steps=[
                PlanStep(
                    id="first",
                    description="Calculate 1 + 1",
                    action=AgentAction(type="tool_call", tool="calculator", arguments={"expression": "1 + 1"}),
                ),
                PlanStep(
                    id="second",
                    description="Calculate 2 + 2",
                    action=AgentAction(type="tool_call", tool="calculator", arguments={"expression": "2 + 2"}),
                ),
            ],
        )

    def replan(self, history, plan, reason, on_reasoning=None) -> ExecutionPlan:
        assert plan.steps[0].status == "completed"
        assert plan.steps[1].status == "pending"
        assert "Human plan feedback" in reason
        return ExecutionPlan(
            goal="Run the revised remaining calculation.",
            steps=[
                PlanStep(
                    id="revised",
                    description="Calculate 3 + 3",
                    action=AgentAction(type="tool_call", tool="calculator", arguments={"expression": "3 + 3"}),
                )
            ],
        )


def test_supplement_uses_remaining_work_replan_after_completed_steps(tmp_path: Path) -> None:
    decisions = iter(
        [
            InterruptDecision("continue"),
            InterruptDecision("supplement", "Use 3 + 3 for the remaining work."),
            InterruptDecision("continue"),
        ]
    )
    state = AgentRunner(FeedbackReplanner(), ToolRegistry(tmp_path), strategy="plan_execute").run(
        "calculate twice",
        lambda _: False,
        interrupt=lambda _request: next(decisions),
    )

    assert state.status == "completed"
    assert state.plan is not None and state.plan.revision == 2
    assert [step.status for step in state.plan_history[0].steps] == ["completed", "superseded"]
    assert state.plan.steps[0].result == "6"
