from pathlib import Path

import pytest

from backend.domain import AssistantMessage, SkillSnapshot, ToolMessage, UserMessage
from backend.runtime import AgentRunner
from backend.runtime.core.contracts import InterruptDecision
from backend.runtime.planning.review import REQUEST_PLAN_REVIEW_NAME, parse_plan_review
from backend.tools import ToolRegistry

PLAN = "# Plan title\n\n## Summary\nMake the requested change."


def review_call(plan: str, call_id: str = "review_1") -> ToolMessage:
    return ToolMessage(
        name=REQUEST_PLAN_REVIEW_NAME,
        call_id=call_id,
        arguments={"plan": plan},
    )


class ScriptedPlanPlanner:
    name = "scripted-plan-review"

    def __init__(self, responses: list[AssistantMessage]) -> None:
        self.responses = list(responses)

    def decide(self, runtime):
        return self.responses.pop(0)


def test_plan_review_parser_accepts_non_empty_markdown_without_enforcing_headings() -> None:
    assert parse_plan_review({"plan": "  Change one thing.  "}) == "Change one thing."


@pytest.mark.parametrize(
    "arguments",
    [
        {"plan": ""},
        {"plan": "   "},
        {},
        {"plan": PLAN, "extra": True},
    ],
)
def test_plan_review_parser_rejects_invalid_arguments(arguments: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        parse_plan_review(arguments)


def test_valid_plan_review_opens_existing_review_and_preserves_one_control_message(tmp_path: Path) -> None:
    planner = ScriptedPlanPlanner([AssistantMessage(tool_messages=[review_call(PLAN)])])
    runner = AgentRunner(planner, ToolRegistry(tmp_path))
    requests = []
    skill = SkillSnapshot("demo", "Demo", "Instructions", ".mini_agent/skills/demo", "abc")
    runtime = runner.new_runtime(
        task="Plan the change",
        mode="plan",
        interrupt=lambda request: requests.append(request) or InterruptDecision("implement"),
        active_skills=[skill],
    )

    result = runner.run(runtime)

    assert result.status == "completed"
    assert result.final_answer == PLAN
    assert result.handoff is not None
    assert result.handoff.task == PLAN
    assert result.handoff.active_skills == (skill,)
    assert [request.kind for request in requests] == ["plan"]
    assert requests[0].data["plan"] == PLAN
    assert runtime.state.messages[0] == UserMessage(content="Plan the change")
    saved = runtime.state.messages[1]
    assert isinstance(saved, AssistantMessage)
    assert saved.content is None
    assert len(saved.tool_messages) == 1
    assert saved.tool_messages[0].arguments == {"plan": PLAN}
    assert saved.tool_messages[0].status == "succeeded"
    assert saved.tool_messages[0].content == "Plan submitted for review."


def test_plan_review_tool_lifecycle_and_review_approval_share_call_id(tmp_path: Path) -> None:
    events = []
    planner = ScriptedPlanPlanner([AssistantMessage(tool_messages=[review_call(PLAN, "review_call")])])
    runner = AgentRunner(planner, ToolRegistry(tmp_path))
    runtime = runner.new_runtime(
        task="Plan the change",
        mode="plan",
        on_event=events.append,
        interrupt=lambda _request: InterruptDecision("implement"),
    )

    runner.run(runtime)

    lifecycle = [
        event
        for event in events
        if event.kind in {"tool_call", "tool_result", "approval_requested", "approval_granted"}
    ]
    assert [event.kind for event in lifecycle] == [
        "tool_call",
        "tool_result",
        "approval_requested",
        "approval_granted",
    ]
    assert [event.data["call_id"] for event in lifecycle] == ["review_call"] * 4


def test_blank_plan_is_retryable_then_valid_plan_opens_review(tmp_path: Path) -> None:
    planner = ScriptedPlanPlanner(
        [
            AssistantMessage(tool_messages=[review_call("   ", "review_bad")]),
            AssistantMessage(tool_messages=[review_call(PLAN, "review_good")]),
        ]
    )
    runner = AgentRunner(planner, ToolRegistry(tmp_path))
    requests = []
    runtime = runner.new_runtime(
        task="Plan the change",
        mode="plan",
        interrupt=lambda request: requests.append(request.kind) or InterruptDecision("stay_in_plan_mode"),
    )

    result = runner.run(runtime)

    assert result.status == "completed"
    assert requests == ["plan"]
    first = runtime.state.messages[1].tool_messages[0]
    second = runtime.state.messages[2].tool_messages[0]
    assert first.status == "failed"
    assert first.retryable is True
    assert "must not be blank" in (first.content or "")
    assert second.status == "succeeded"
    assert len(result.actions) == 2


def test_plan_review_must_not_be_mixed_with_execution_tools(tmp_path: Path) -> None:
    planner = ScriptedPlanPlanner(
        [
            AssistantMessage(
                tool_messages=[
                    review_call(PLAN),
                    ToolMessage(
                        name="run_command",
                        call_id="command_1",
                        arguments={"command": "Get-ChildItem"},
                    ),
                ]
            ),
            AssistantMessage(content="I can explain the options without opening review."),
        ]
    )
    runner = AgentRunner(planner, ToolRegistry(tmp_path))
    runtime = runner.new_runtime(
        task="Discuss the change",
        mode="plan",
        interrupt=lambda _request: pytest.fail("mixed control call must be returned to the model"),
    )

    result = runner.run(runtime)

    assert result.status == "completed"
    assert result.final_answer == "I can explain the options without opening review."
    rejected = runtime.state.messages[1]
    assert isinstance(rejected, AssistantMessage)
    assert [tool.status for tool in rejected.tool_messages] == ["failed", "failed"]
    assert all(tool.retryable is True for tool in rejected.tool_messages)
    assert all("only tool call" in (tool.content or "") for tool in rejected.tool_messages)


def test_repeated_invalid_plan_review_calls_continue_until_model_recovers(tmp_path: Path) -> None:
    planner = ScriptedPlanPlanner(
        [
            *[AssistantMessage(tool_messages=[review_call(" ", f"review_{index}")]) for index in range(5)],
            AssistantMessage(content="The plan could not be prepared."),
        ]
    )
    runner = AgentRunner(planner, ToolRegistry(tmp_path))
    runtime = runner.new_runtime(
        task="Plan the change",
        mode="plan",
        interrupt=lambda _request: pytest.fail("invalid plan must not open review"),
    )

    result = runner.run(runtime)

    assert result.status == "completed"
    assert result.final_answer == "The plan could not be prepared."
    assert len(result.actions) == 5


class StreamingPlanPlanner:
    name = "streaming-plan-review"

    def decide(self, runtime) -> AssistantMessage:
        assert runtime.exchange.on_content is not None
        runtime.exchange.on_content(PLAN)
        return AssistantMessage(content=PLAN, tool_messages=[review_call(PLAN)])


def test_streamed_plan_with_tool_call_is_retained_and_final_plan_is_marked_streamed(
    tmp_path: Path,
) -> None:
    events = []
    runner = AgentRunner(StreamingPlanPlanner(), ToolRegistry(tmp_path))
    runtime = runner.new_runtime(
        task="Plan the change",
        mode="plan",
        on_event=events.append,
        interrupt=lambda _request: InterruptDecision("implement"),
    )

    result = runner.run(runtime)

    assert result.status == "completed"
    assert runtime.state.messages[1].content == PLAN
    assert [event.kind for event in events].count("response_delta") == 1
    final = next(event for event in events if event.kind == "plan")
    assert final.message == PLAN
    assert final.data["streamed"] is True
