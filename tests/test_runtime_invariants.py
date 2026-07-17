from mini_agent.domain import AssistantMessage, ToolMessage
from mini_agent.runtime import AgentRunner, AgentRuntime
from mini_agent.runtime.contracts import InterruptDecision
from mini_agent.runtime.plan_review import REQUEST_PLAN_REVIEW_NAME
from mini_agent.tools import Tool, ToolRegistry


class RepeatingReplyPlanner:
    name = "repeating-reply"

    def decide(self, runtime: AgentRuntime) -> AssistantMessage:
        return AssistantMessage(content="Same reply.")


def test_equal_assistant_replies_are_appended_on_every_turn() -> None:
    runner = AgentRunner(RepeatingReplyPlanner(), ToolRegistry(), strategy="reactive")
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
        strategy="reactive",
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


def test_plan_review_defaults_to_cancel_without_an_interrupt_handler() -> None:
    runner = AgentRunner(PlanProposalPlanner(), ToolRegistry())
    runtime = runner.new_runtime(
        task="prepare a plan",
        mode="plan",
        confirm=lambda _message: True,
    )

    result = runner.run(runtime)

    assert result.status == "cancelled"
    assert not any(event.kind == "approval_granted" for event in result.events)


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
