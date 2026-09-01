import pytest

from backend.domain import AssistantMessage, PlanningError, ToolMessage
from backend.runtime import AgentRunner, AgentRuntime
from backend.runtime.core.contracts import InterruptDecision
from backend.runtime.execution.steps import ToolStepResult
from backend.runtime.execution.workflows import execution as execution_workflow
from backend.runtime.execution.workflows import proposal as proposal_workflow
from backend.runtime.planning.review import REQUEST_PLAN_REVIEW_NAME
from backend.tools import Tool, ToolRegistry


class RepeatingReplyPlanner:
    name = "repeating-reply"

    def decide(self, runtime: AgentRuntime) -> AssistantMessage:
        return AssistantMessage(content="Same reply.")


def test_equal_assistant_replies_are_appended_on_every_turn() -> None:
    runner = AgentRunner(RepeatingReplyPlanner(), ToolRegistry())
    first_runtime = runner.new_runtime(task="first turn")

    runner.run(first_runtime)
    second_runtime = runner.new_runtime(task="second turn", messages=first_runtime.state.messages)
    runner.run(second_runtime)

    replies = [
        message
        for message in second_runtime.state.messages
        if isinstance(message, AssistantMessage) and message.content == "Same reply."
    ]
    assert len(replies) == 2


class UsagePlanner:
    name = "usage"

    def decide(self, runtime: AgentRuntime) -> AssistantMessage:
        runtime.state.turn_usage = {"total_tokens": 7}
        return AssistantMessage(content="Done.")


class FinishSnapshotStore:
    def __init__(self) -> None:
        self.snapshot: dict[str, object] | None = None

    def save(self, runtime: AgentRuntime, reason: str) -> None:
        if reason != "run_finished":
            return
        self.snapshot = {
            "usage": runtime.state.usage,
            "turn_usage": runtime.state.turn_usage,
            "status": runtime.state.status,
            "run_history": [(summary.run_id, summary.status) for summary in runtime.state.run_history],
        }


def test_run_finished_checkpoint_observes_archived_runtime_state() -> None:
    checkpoints = FinishSnapshotStore()
    runner = AgentRunner(
        UsagePlanner(),
        ToolRegistry(),
        checkpoints=checkpoints,
    )
    runtime = runner.new_runtime(task="record usage")

    result = runner.run(runtime)

    assert checkpoints.snapshot == {
        "usage": {"total_tokens": 7},
        "turn_usage": None,
        "status": "idle",
        "run_history": [(result.run_id, "completed")],
    }


class RootFailurePlanner:
    name = "root-failure"

    def decide(self, runtime: AgentRuntime) -> AssistantMessage:
        del runtime
        try:
            raise ConnectionError("provider socket closed")
        except ConnectionError as exc:
            raise PlanningError("planner wrapper") from exc


@pytest.mark.parametrize("mode", ["agent", "plan"])
def test_planning_workflows_publish_only_the_root_failure_message(mode: str) -> None:
    events = []
    runner = AgentRunner(RootFailurePlanner(), ToolRegistry())
    runtime = runner.new_runtime(
        task="fail",
        mode=mode,
        on_event=events.append,
    )

    result = runner.run(runtime)

    assert result.status == "failed"
    assert result.final_answer == "provider socket closed"
    assert next(event.message for event in events if event.kind == "error") == "provider socket closed"


class PlanProposalPlanner:
    name = "plan-proposal"

    def decide(self, runtime: AgentRuntime) -> AssistantMessage:
        return AssistantMessage(
            tool_messages=[
                ToolMessage(
                    name=REQUEST_PLAN_REVIEW_NAME,
                    call_id="review_1",
                    arguments={"plan": "1. Inspect the project."},
                )
            ]
        )


def test_plan_review_defaults_to_staying_in_plan_mode_without_an_interrupt_handler() -> None:
    runner = AgentRunner(PlanProposalPlanner(), ToolRegistry())
    events = []
    runtime = runner.new_runtime(
        task="prepare a plan",
        mode="plan",
        confirm=lambda _message: True,
        on_event=events.append,
    )

    result = runner.run(runtime)

    assert result.status == "completed"
    assert result.mode == "plan"
    assert any(event.kind == "approval_granted" for event in events)


class ToolPlanProposalPlanner:
    name = "tool-plan-proposal"

    def decide(self, runtime: AgentRuntime) -> AssistantMessage:
        return AssistantMessage(tool_messages=[ToolMessage(name="inspect", call_id="call_1")])


def test_plan_proposal_tool_interrupt_clears_active_tool_state() -> None:
    tool = Tool(
        "inspect",
        "Inspect the project",
        lambda: "inspected",
        requires_confirmation=True,
    )
    runner = AgentRunner(ToolPlanProposalPlanner(), ToolRegistry([tool]))
    runtime = runner.new_runtime(
        task="prepare a plan",
        mode="plan",
        interrupt=lambda _request: InterruptDecision("cancel"),
    )

    result = runner.run(runtime)

    assert result.status == "cancelled"
    assert runtime.state.active_message is None
    assert runtime.state.active_tool_index is None


class RecoveringFailurePlanner:
    name = "recovering-failure"

    def __init__(self, tool_name: str) -> None:
        self.tool_name = tool_name
        self.calls = 0

    def decide(self, runtime: AgentRuntime) -> AssistantMessage:
        del runtime
        self.calls += 1
        if self.calls == 1:
            return AssistantMessage(tool_messages=[ToolMessage(name=self.tool_name, call_id="failure_1")])
        return AssistantMessage(content="Recovered.")


@pytest.mark.parametrize("mode", ["agent", "plan"])
@pytest.mark.parametrize(
    ("tool_name", "expected"),
    [
        ("run_command", "Command exited with code 7.\nstdout:\n0\n\nstderr:\nbad"),
        ("inspect", "Command exited with code 7.\nstdout:\n0\n\nstderr:\nbad"),
    ],
)
def test_workflows_only_preserve_unwrapped_run_command_failures(
    mode: str,
    tool_name: str,
    expected: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = "Command exited with code 7.\nstdout:\n0\n\nstderr:\nbad"

    def fail_without_invoking_hooks(runtime: AgentRuntime, index: int, _executor: object) -> ToolStepResult:
        message = runtime.state.active_message
        assert message is not None
        tool = message.tool_messages[index]
        tool.status = "failed"
        tool.content = error
        runtime.state.active_tool_index = index
        runtime.run.actions.append(tool)
        return ToolStepResult(success=False, error=error)

    workflow = execution_workflow if mode == "agent" else proposal_workflow
    monkeypatch.setattr(workflow, "_execute_tool", fail_without_invoking_hooks)

    planner = RecoveringFailurePlanner(tool_name)
    runner = AgentRunner(planner, ToolRegistry([Tool(tool_name, "Unused", lambda: "unused")]))
    runtime = runner.new_runtime(task="Recover from the failure", mode=mode)

    result = runner.run(runtime)

    assert result.status == "completed"
    failed_message = next(
        message for message in runtime.state.messages if isinstance(message, AssistantMessage) and message.tool_messages
    )
    assert failed_message.tool_messages[0].content == expected
