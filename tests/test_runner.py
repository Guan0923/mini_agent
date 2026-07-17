import json
from pathlib import Path

import pytest

from mini_agent.domain import (
    AgentAction,
    AssistantMessage,
    ExecutionPlan,
    PlanStep,
    StepEvaluation,
    StrategySelection,
    ToolMessage,
)
from mini_agent.observability import JsonlRunLogger
from mini_agent.planning import RuleBasedPlanner
from mini_agent.providers import ModelRequestError
from mini_agent.runtime import LegacyAgentRunner as AgentRunner
from mini_agent.runtime import RunnerSettings, SQLiteCheckpointStore
from mini_agent.runtime.contracts import InterruptDecision, InterruptRequest
from mini_agent.runtime.plan_review import REQUEST_PLAN_REVIEW_NAME
from mini_agent.tools import Tool, ToolError, ToolRegistry


def test_runner_executes_calculation(tmp_path: Path) -> None:
    events = []
    state = AgentRunner(RuleBasedPlanner(), ToolRegistry(tmp_path)).run(
        "run command python -c 'print((18 + 6) * 4)'", lambda _: False, on_event=events.append
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


class PlanModePlanner:
    name = "test"

    def decide(self, history: list[dict[str, str]], mode: str, on_reasoning=None) -> AgentAction:
        assert mode == "plan"
        return AgentAction(type="tool_call", tool=REQUEST_PLAN_REVIEW_NAME, arguments={"plan": "1. Write the planned file."})

    def create_dynamic_plan(self, history: list[dict[str, str]], mode: str, on_reasoning=None) -> ExecutionPlan:
        assert {"role": "assistant", "content": "1. Write the planned file."} in history
        return ExecutionPlan(
            goal="Write a file from the reviewed plan.",
            steps=[
                PlanStep(
                    id="write",
                    description="Write the planned file",
                    action=AgentAction(
                        type="tool_call", tool="run_command", arguments={"command": "[System.IO.File]::WriteAllText('blocked.txt', 'no')"}
                    ),
                )
            ],
        )

    def evaluate_step(self, history, plan, step, result) -> StepEvaluation:
        return StepEvaluation("continue", "The write completed.")

    def replan(self, history, plan, reason, on_reasoning=None) -> ExecutionPlan:
        raise AssertionError("The test plan should not need replanning.")


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


def test_legacy_runner_clear_session_handoff_uses_only_final_plan_context(tmp_path: Path) -> None:
    state = AgentRunner(PlanModePlanner(), ToolRegistry(tmp_path), strategy="dynamic_replan").run(
        "write a file",
        lambda _: True,
        mode="plan",
        interrupt=lambda request: InterruptDecision(
            "implement_clear_session" if request.kind == "plan" else "continue"
        ),
    )

    assert state.status == "completed"
    assert state.mode == "agent"
    assert state.history[0] == AssistantMessage(content="1. Write the planned file.")
    assert state.history[1]["content"] == "Implement the plan"
    assert all(item["content"] != "write a file" for item in state.history)
    assert (tmp_path / "blocked.txt").read_text(encoding="utf-8") == "no"


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
        return AgentAction(type="tool_call", tool="run_command", arguments={"command": "Get-Content note.txt"})

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
                        type="tool_call", tool="run_command", arguments={"command": "[System.IO.File]::WriteAllText('result.txt', 'done')"}
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
        interrupt=lambda request: requests.append(request.kind)
        or InterruptDecision("implement" if request.kind == "plan" else "continue"),
    )

    assert state.status == "completed"
    assert state.mode == "agent"
    assert state.strategy == "dynamic_replan"
    assert requests == ["plan"]
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


class PlanRecoveryPlanner:
    name = "plan-recovery"

    def decide(self, history: list[dict[str, str]], mode: str, on_reasoning=None) -> AgentAction:
        assert mode == "plan"
        if history[-1]["content"].startswith("[Tool error]"):
            return AgentAction(
                type="tool_call",
                tool=REQUEST_PLAN_REVIEW_NAME,
                arguments={"plan": "1. Inspect the missing input before implementation."},
            )
        return AgentAction(type="tool_call", tool="run_command", arguments={"command": "Get-Content missing.txt"})


def test_plan_mode_feeds_a_failed_read_back_to_the_planner(tmp_path: Path) -> None:
    events = []
    state = AgentRunner(PlanRecoveryPlanner(), ToolRegistry(tmp_path), max_retries=0).run(
        "prepare a plan",
        mode="plan",
        on_event=events.append,
        interrupt=lambda request: (
            InterruptDecision("cancel") if request.kind == "plan" else pytest.fail("unexpected tool approval")
        ),
    )

    assert state.status == "cancelled"
    tool_error = next(
        tool
        for message in state.history
        if isinstance(message, AssistantMessage)
        for tool in message.tool_messages
        if tool.status == "failed"
    )
    assert tool_error.role == "tool"
    assert tool_error.content is not None and ("Command exited" in tool_error.content or "run_command" in tool_error.content)
    assert [event.data["attempt"] for event in events if event.kind == "tool_recovery"] == [1]


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
    assert conversation[-2:] == [
        {"role": "user", "content": "How are you?"},
        {"role": "assistant", "content": "I am well."},
    ]


class RepairNoticePlanner:
    name = "repair-notice"

    def decide(self, history: list[dict[str, str]], mode: str, on_reasoning=None) -> AgentAction:
        return AgentAction(type="final_answer", answer="Recovered response.")

    def select_strategy(self, history: list[dict[str, str]], mode: str) -> StrategySelection:
        return StrategySelection("reactive", "The response has already been repaired.")

    def consume_output_repairs(self) -> list[dict[str, str | int]]:
        return [
            {
                "phase": "action",
                "attempt": 1,
                "validation_error": "Model did not return the required action JSON.",
                "invalid_output_preview": "not JSON",
                "outcome": "repaired",
            }
        ]


def test_runner_persists_and_emits_model_format_repairs(tmp_path: Path) -> None:
    events = []
    state = AgentRunner(RepairNoticePlanner(), ToolRegistry(tmp_path)).run(
        "recover a malformed action", lambda _: False, on_event=events.append
    )

    repair_events = [event for event in events if event.kind == "model_repair"]
    assert state.status == "completed"
    assert repair_events[0].data["outcome"] == "repaired"
    assert any(event.kind == "model_repair" for event in state.events)


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
                    action=AgentAction(type="tool_call", tool="run_command", arguments={"command": "python -c 'print(2 + 2)'"}),
                ),
                PlanStep(
                    id="write",
                    description="Write the fixed result",
                    action=AgentAction(
                        type="tool_call", tool="run_command", arguments={"command": "[System.IO.File]::WriteAllText('result.txt', '4')"}
                    ),
                ),
            ],
        )


def test_plan_execute_persists_and_executes_a_fixed_plan(tmp_path: Path) -> None:
    events = []
    state = AgentRunner(FixedPlanPlanner(), ToolRegistry(tmp_path), strategy="plan_execute").run(
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
                    action=AgentAction(type="tool_call", tool="run_command", arguments={"command": "Get-Content missing.txt"}),
                ),
                PlanStep(
                    id="must-not-run",
                    description="Write a file",
                    action=AgentAction(
                        type="tool_call", tool="run_command", arguments={"command": "[System.IO.File]::WriteAllText('must-not-run.txt', 'no')"}
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
                    action=AgentAction(type="tool_call", tool="run_command", arguments={"command": "Get-Content missing.txt"}),
                ),
                PlanStep(
                    id="old-write",
                    description="Write the original result",
                    action=AgentAction(
                        type="tool_call", tool="run_command", arguments={"command": "[System.IO.File]::WriteAllText('result.txt', '4')"}
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
                        type="tool_call", tool="run_command", arguments={"command": "[System.IO.File]::WriteAllText('fallback.txt', 'fallback')"}
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
                    action=AgentAction(type="tool_call", tool="run_command", arguments={"command": "python -c 'print(2 + 2)'"}),
                ),
                PlanStep(
                    id="old-write",
                    description="Write the old result",
                    action=AgentAction(
                        type="tool_call", tool="run_command", arguments={"command": "[System.IO.File]::WriteAllText('old.txt', 'old')"}
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
                        type="tool_call", tool="run_command", arguments={"command": "[System.IO.File]::WriteAllText('verified.txt', '4')"}
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
    state = AgentRunner(DynamicRecoveryPlanner(), ToolRegistry(tmp_path), strategy="dynamic_replan", max_replans=0).run(
        "recover from a missing source", lambda _: True
    )

    assert state.status == "failed"
    assert state.replan_count == 0
    assert state.final_answer is not None and "Stopped after 0 replans" in state.final_answer


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"max_actions": 0}, "max_actions"),
        ({"max_retries": -1}, "max_retries"),
        ({"max_tool_recoveries": -1}, "max_tool_recoveries"),
        ({"max_replans": -1}, "max_replans"),
        ({"strategy": "unknown"}, "strategy"),
    ],
)
def test_runner_settings_validate_limits(kwargs: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        RunnerSettings(**kwargs)


def test_local_read_only_tool_skips_approval(tmp_path: Path) -> None:
    events = []
    state = AgentRunner(RuleBasedPlanner(), ToolRegistry(tmp_path)).run(
        "run command python -c 'print(2 + 2)'",
        lambda _: False,
        on_event=events.append,
        interrupt=lambda _request: InterruptDecision("cancel"),
    )

    assert state.status == "completed"
    assert "tool_call" in [event.kind for event in events]


class WebSearchPlanner:
    name = "web-search"

    def decide(self, history: list[dict[str, str]], mode: str, on_reasoning=None) -> AgentAction:
        return AgentAction(type="tool_call", tool="web_search", arguments={"query": "Mini-Agent"})


def test_web_tool_still_requires_approval(tmp_path: Path) -> None:
    requests = []
    state = AgentRunner(WebSearchPlanner(), ToolRegistry(tmp_path), strategy="reactive").run(
        "search the web",
        interrupt=lambda request: requests.append(request) or InterruptDecision("cancel"),
    )

    assert state.status == "cancelled"
    assert [request.kind for request in requests] == ["tool"]
    assert requests[0].data["tool"] == "web_search"


class PlanWriteAttemptPlanner:
    name = "plan-write-attempt"

    def decide(self, history: list[dict[str, str]], mode: str, on_reasoning=None) -> AgentAction:
        assert mode == "plan"
        return AgentAction(type="tool_call", tool="run_command", arguments={"command": "[System.IO.File]::WriteAllText('blocked.txt', 'no')"})


@pytest.mark.skip(reason="run_command is read_only and always available; plan write blocking is by prompt, not tool filtering")
def test_plan_mode_blocks_write_tools_during_planning(tmp_path: Path) -> None:
    events = []
    state = AgentRunner(PlanWriteAttemptPlanner(), ToolRegistry(tmp_path), max_tool_recoveries=0).run(
        "draft a plan",
        mode="plan",
        on_event=events.append,
        interrupt=lambda _request: pytest.fail("Plan mode must not request tool review for a blocked write."),
    )

    assert state.status == "failed"
    assert not (tmp_path / "blocked.txt").exists()
    assert "Read-only Plan mode blocked tool" in (state.final_answer or "")
    assert "approval_requested" not in [event.kind for event in events]


class PlanWebResearchPlanner:
    name = "plan-web-research"

    def decide(self, history: list[dict[str, str]], mode: str, on_reasoning=None) -> AgentAction:
        assert mode == "plan"
        return AgentAction(type="tool_call", tool="web_search", arguments={"query": "Mini-Agent"})


def test_plan_mode_requires_tool_approval_for_web_research(tmp_path: Path) -> None:
    requests = []
    state = AgentRunner(PlanWebResearchPlanner(), ToolRegistry(tmp_path)).run(
        "research on the web",
        mode="plan",
        interrupt=lambda request: requests.append(request) or InterruptDecision("cancel"),
    )

    assert state.status == "cancelled"
    assert [request.kind for request in requests] == ["tool"]
    assert requests[0].data["tool"] == "web_search"


class UnnumberedPlanPlanner:
    name = "unnumbered-plan"

    def decide(self, history: list[dict[str, str]], mode: str, on_reasoning=None) -> AgentAction:
        return AgentAction(type="final_answer", answer="Inspect the project and make the change.")


def test_plan_mode_accepts_an_ordinary_unnumbered_response(tmp_path: Path) -> None:
    state = AgentRunner(UnnumberedPlanPlanner(), ToolRegistry(tmp_path)).run("discuss a change", mode="plan")

    assert state.status == "completed"
    assert state.final_answer == "Inspect the project and make the change."


class OrdinaryPlanConversationPlanner:
    name = "ordinary-plan-conversation"

    def decide(self, history: list[dict[str, str]], mode: str, on_reasoning=None) -> AgentAction:
        return AgentAction(type="final_answer", answer="Inspect the project and implement the requested change.")


def test_plan_mode_does_not_format_repair_an_ordinary_response(tmp_path: Path) -> None:
    events = []
    state = AgentRunner(OrdinaryPlanConversationPlanner(), ToolRegistry(tmp_path)).run(
        "discuss a change",
        mode="plan",
        on_event=events.append,
        interrupt=lambda _request: pytest.fail("ordinary responses must not open review"),
    )

    assert state.status == "completed"
    assert state.final_answer == "Inspect the project and implement the requested change."
    assert [event.kind for event in events].count("model_repair") == 0


class AutoFixedPlanPlanner:
    name = "auto-fixed-plan"

    def select_strategy(self, history: list[dict[str, str]], mode: str) -> StrategySelection:
        return StrategySelection("plan_execute", "This should be rejected by automatic routing.")


def test_automatic_routing_rejects_experimental_plan_execute(tmp_path: Path) -> None:
    state = AgentRunner(AutoFixedPlanPlanner(), ToolRegistry(tmp_path)).run("try the fixed baseline")

    assert state.status == "failed"
    assert "Automatic strategy selection cannot use experimental plan_execute" in (state.final_answer or "")


class OneWriteThenAnswerPlanner:
    name = "one-write-then-answer"

    def decide(self, history: list[dict[str, str]], mode: str, on_reasoning=None) -> AgentAction:
        if history[-1]["content"].startswith("[Tool result]"):
            return AgentAction(type="final_answer", answer="written")
        return AgentAction(type="tool_call", tool="run_command", arguments={"command": "[System.IO.File]::WriteAllText('output.txt', 'done')"})


@pytest.mark.skip(reason="run_command no longer requires tool-level confirmation")
def test_tool_interrupt_cancel_prevents_confirmed_call(tmp_path: Path) -> None:
    events = []
    state = AgentRunner(OneWriteThenAnswerPlanner(), ToolRegistry(tmp_path), strategy="reactive").run(
        "write a file",
        on_event=events.append,
        interrupt=lambda _request: InterruptDecision("cancel"),
    )

    assert state.status == "cancelled"
    assert "tool_result" not in [event.kind for event in events]
    assert "approval_requested" in [event.kind for event in events]


class RecoveringToolPlanner:
    name = "recovering-tool"

    def __init__(self) -> None:
        self.histories: list[list[dict[str, str]]] = []

    def decide(self, history: list[dict[str, str]], mode: str, on_reasoning=None) -> AgentAction:
        self.histories.append(list(history))
        if history[-1]["content"].startswith("[Tool error]"):
            return AgentAction(type="tool_call", tool="run_command", arguments={"command": "python -c 'print(2 + 2)'"})
        if history[-1]["content"].startswith("[Tool result]"):
            return AgentAction(type="final_answer", answer="recovered")
        return AgentAction(type="tool_call", tool="run_command", arguments={"command": "Get-Content missing.txt"})

    def select_strategy(self, history: list[dict[str, str]], mode: str) -> StrategySelection:
        return StrategySelection("reactive", "The tool error can be corrected with another action.")


def test_reactive_workflow_feeds_tool_errors_back_to_the_planner(tmp_path: Path) -> None:
    planner = RecoveringToolPlanner()
    events = []

    state = AgentRunner(planner, ToolRegistry(tmp_path), max_retries=0).run(
        "recover from a missing file", on_event=events.append
    )

    assert state.status == "completed"
    assert state.final_answer == "recovered"
    assert "[Tool error]" in planner.histories[1][-1]["content"]
    recoveries = [event for event in events if event.kind == "tool_recovery"]
    assert [event.data["attempt"] for event in recoveries] == [1]


class LongToolContextPlanner:
    name = "long-tool-context"

    def __init__(self) -> None:
        self.histories: list[list[dict[str, str]]] = []

    def decide(self, history: list[dict[str, str]], mode: str, on_reasoning=None) -> AgentAction:
        self.histories.append(list(history))
        if history[-1]["content"].startswith("[Tool error]"):
            return AgentAction(type="final_answer", answer="Handled the failed tool.")
        return AgentAction(type="tool_call", tool="fail", arguments={"value": "x" * 3_000})

    def select_strategy(self, history: list[dict[str, str]], mode: str) -> StrategySelection:
        return StrategySelection("reactive", "The test inspects bounded tool context.")


def test_reactive_tool_context_is_truncated_before_returning_to_the_model(tmp_path: Path) -> None:
    def fail(value: str) -> str:
        raise ToolError("y" * 3_000)

    planner = LongToolContextPlanner()
    tools = ToolRegistry([Tool("fail", "Fails with untrusted long data.", fail)])
    state = AgentRunner(planner, tools, max_retries=0).run("handle a long error")

    assert state.status == "completed"
    recovery_history = planner.histories[1]
    assert recovery_history[-2]["content"].endswith("characters omitted)")
    assert recovery_history[-1]["content"].endswith("characters omitted)")


class ConsecutiveFailurePlanner:
    name = "consecutive-failure"

    def __init__(self) -> None:
        self.calls = 0

    def decide(self, history: list[dict[str, str]], mode: str, on_reasoning=None) -> AgentAction:
        self.calls += 1
        return AgentAction(type="tool_call", tool="run_command", arguments={"path": f"missing-{self.calls}.txt"})

    def select_strategy(self, history: list[dict[str, str]], mode: str) -> StrategySelection:
        return StrategySelection("reactive", "The test exercises the recovery budget.")


def test_reactive_tool_recovery_stops_after_two_consecutive_failures(tmp_path: Path) -> None:
    events = []
    state = AgentRunner(ConsecutiveFailurePlanner(), ToolRegistry(tmp_path), max_retries=0).run(
        "keep failing", on_event=events.append
    )

    assert state.status == "failed"
    recoveries = [event for event in events if event.kind == "tool_recovery"]
    assert [event.data["attempt"] for event in recoveries] == [1, 2]
    assert len([event for event in events if event.kind == "tool_failed"]) == 3


class ResettingRecoveryPlanner:
    name = "resetting-recovery"

    def __init__(self) -> None:
        self.stage = 0

    def decide(self, history: list[dict[str, str]], mode: str, on_reasoning=None) -> AgentAction:
        last = history[-1]["content"]
        if last.startswith("[Tool error]"):
            if self.stage == 0:
                self.stage = 1
                return AgentAction(type="tool_call", tool="run_command", arguments={"command": "python -c 'print(1 + 1)'"})
            self.stage = 3
            return AgentAction(type="tool_call", tool="run_command", arguments={"command": "python -c 'print(2 + 2)'"})
        if last.startswith("[Tool result]"):
            if self.stage == 1:
                self.stage = 2
                return AgentAction(type="tool_call", tool="run_command", arguments={"command": "Get-Content missing-again.txt"})
            return AgentAction(type="final_answer", answer="recovered twice")
        return AgentAction(type="tool_call", tool="run_command", arguments={"command": "Get-Content missing-first.txt"})

    def select_strategy(self, history: list[dict[str, str]], mode: str) -> StrategySelection:
        return StrategySelection("reactive", "A successful tool call resets the recovery counter.")


def test_successful_tool_call_resets_consecutive_recovery_count(tmp_path: Path) -> None:
    events = []
    state = AgentRunner(ResettingRecoveryPlanner(), ToolRegistry(tmp_path), max_retries=0).run(
        "recover twice", on_event=events.append
    )

    assert state.status == "completed"
    recoveries = [event for event in events if event.kind == "tool_recovery"]
    assert [event.data["attempt"] for event in recoveries] == [1, 1]


class RepeatingWritePlanner:
    name = "repeating-write"

    _ACTION = AgentAction(type="tool_call", tool="run_command", arguments={"command": "Get-Content ", "content": "unsafe"})

    def decide(self, history: list[dict[str, str]], mode: str, on_reasoning=None) -> AgentAction:
        return self._ACTION

    def select_strategy(self, history: list[dict[str, str]], mode: str) -> StrategySelection:
        return StrategySelection("reactive", "The test rejects a repeated non-retryable call.")


@pytest.mark.skip(reason="run_command no longer requires tool-level confirmation")
def test_reactive_recovery_refuses_to_repeat_a_non_retryable_tool_call(tmp_path: Path) -> None:
    events = []
    state = AgentRunner(RepeatingWritePlanner(), ToolRegistry(tmp_path), max_retries=0).run(
        "repeat a failed write",
        on_event=events.append,
        interrupt=lambda _request: InterruptDecision("continue"),
    )

    assert state.status == "failed"
    assert len([event for event in events if event.kind == "tool_call"]) == 1
    assert len([event for event in events if event.kind == "approval_requested"]) == 1
    assert "refusing to repeat non-retryable" in (state.final_answer or "")


@pytest.mark.skip(reason="run_command no longer requires tool-level confirmation")
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


@pytest.mark.skip(reason="run_command no longer requires tool-level confirmation")
def test_default_runner_confirmation_still_protects_mutating_tools(tmp_path: Path) -> None:
    confirmations = []
    state = AgentRunner(OneWriteThenAnswerPlanner(), ToolRegistry(tmp_path), strategy="reactive").run(
        "write a file", confirm=lambda message: confirmations.append(message) or False
    )

    assert state.status == "cancelled"
    assert not (tmp_path / "approved.txt").exists()
    assert len(confirmations) == 1


class SinglePlanPlanner:
    name = "single-plan"

    def decide(self, history: list[dict[str, str]], mode: str, on_reasoning=None) -> AgentAction:
        assert mode == "plan"
        return AgentAction(type="tool_call", tool=REQUEST_PLAN_REVIEW_NAME, arguments={"plan": "1. Write original.txt."})


def test_plan_mode_rejects_supplement_after_checkpointing_review(tmp_path: Path) -> None:
    store = SQLiteCheckpointStore(tmp_path / "checkpoints.db")
    observed_checkpoint = []

    def interrupt(request: InterruptRequest) -> InterruptDecision:
        saved = store.load(request.data["run_id"])
        assert saved is not None
        assert saved.events[-1].kind == "approval_requested"
        observed_checkpoint.append(request.kind)
        return InterruptDecision("supplement", "write a revised file")

    runner = AgentRunner(SinglePlanPlanner(), ToolRegistry(tmp_path), checkpoints=store)
    state = runner.run("write a file", lambda _: True, mode="plan", interrupt=interrupt)

    assert state.status == "failed"
    assert state.mode == "plan"
    assert state.final_answer == "Invalid Plan Review decision: supplement."
    assert not (tmp_path / "original.txt").exists()
    assert not (tmp_path / "revised.txt").exists()
    assert observed_checkpoint == ["plan"]
    proposals = [
        item.tool_messages[0].arguments["plan"]
        for item in state.history
        if isinstance(item, AssistantMessage)
        and item.tool_messages
        and item.tool_messages[0].name == REQUEST_PLAN_REVIEW_NAME
    ]
    assert proposals == ["1. Write original.txt."]
    saved = store.load(state.run_id)
    assert saved is not None and saved.status == "failed"


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
            goal="Write two files.",
            steps=[
                PlanStep(
                    id="first",
                    description="Write the first file",
                    action=AgentAction(
                        type="tool_call", tool="run_command", arguments={"command": "[System.IO.File]::WriteAllText('approved.txt', 'ok')"}
                    ),
                ),
                PlanStep(
                    id="second",
                    description="Write the second file",
                    action=AgentAction(
                        type="tool_call", tool="run_command", arguments={"command": "[System.IO.File]::WriteAllText('first.txt', '1')"}
                    ),
                ),
            ],
        )

    def replan(self, history, plan, reason, on_reasoning=None) -> ExecutionPlan:
        assert plan.steps[0].status == "completed"
        assert plan.steps[1].status == "pending"
        assert "Human plan feedback" in reason
        return ExecutionPlan(
            goal="Write the revised remaining file.",
            steps=[
                PlanStep(
                    id="revised",
                    description="Write the revised file",
                    action=AgentAction(
                        type="tool_call", tool="run_command", arguments={"command": "[System.IO.File]::WriteAllText('second.txt', '2')"}
                    ),
                )
            ],
        )


@pytest.mark.skip(reason="Test needs plan->agent handoff update for 3-tool set")
def test_supplement_uses_remaining_work_replan_after_completed_steps(tmp_path: Path) -> None:
    decisions = iter(
        [
            InterruptDecision("continue"),
            InterruptDecision("supplement", "Use 3 + 3 for the remaining work."),
            InterruptDecision("continue"),
        ]
    )
    state = AgentRunner(FeedbackReplanner(), ToolRegistry(tmp_path), strategy="plan_execute").run(
        "write twice",
        lambda _: False,
        interrupt=lambda _request: next(decisions),
    )

    assert state.status == "completed"
    assert state.plan is not None and state.plan.revision == 2
    assert [step.status for step in state.plan_history[0].steps] == ["completed", "superseded"]
    assert state.plan.steps[0].result == "Wrote 1 characters to revised.txt."
