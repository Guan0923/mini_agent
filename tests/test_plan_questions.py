import json
from pathlib import Path

import pytest

from backend.domain import AssistantMessage, ToolMessage, UserMessage
from backend.runtime import AgentRunner, ConversationService
from backend.runtime.conversation.user_input import (
    REQUEST_USER_INPUT_NAME,
    format_user_input_answers,
    parse_user_input_questions,
)
from backend.runtime.core.contracts import InterruptDecision, QuestionOption, UserQuestion
from backend.runtime.planning.review import REQUEST_PLAN_REVIEW_NAME
from backend.tools import ToolRegistry
from tests.local_store import session_store


def question_arguments() -> dict[str, object]:
    return {
        "questions": [
            {
                "id": "storage",
                "header": "Storage",
                "question": "Where should the result be stored?",
                "options": [
                    {"label": "PostgreSQL", "description": "Keep the data in the existing database."},
                    {"label": "JSONL", "description": "Write the data to the existing audit stream."},
                ],
            }
        ]
    }


def test_request_user_input_parser_builds_typed_questions() -> None:
    questions = parse_user_input_questions(question_arguments())

    assert questions == (
        UserQuestion(
            id="storage",
            header="Storage",
            question="Where should the result be stored?",
            options=(
                QuestionOption("PostgreSQL", "Keep the data in the existing database."),
                QuestionOption("JSONL", "Write the data to the existing audit stream."),
            ),
        ),
    )


def test_request_user_input_parser_rejects_duplicate_question_ids() -> None:
    arguments = question_arguments()
    arguments["questions"] = [*arguments["questions"], arguments["questions"][0]]

    with pytest.raises(ValueError, match="unique"):
        parse_user_input_questions(arguments)


def test_request_user_input_parser_filters_exact_client_other_label() -> None:
    arguments = question_arguments()
    arguments["questions"][0]["options"].insert(
        0,
        {"label": "  其他  ", "description": "Provide a custom answer."},
    )

    questions = parse_user_input_questions(arguments)

    assert [option.label for option in questions[0].options] == ["PostgreSQL", "JSONL"]


def test_user_input_answers_use_codex_style_tool_result_shape() -> None:
    result = format_user_input_answers({"storage": ["PostgreSQL"]})

    assert json.loads(result) == {"answers": {"storage": {"answers": ["PostgreSQL"]}}}


class QuestionThenPlanPlanner:
    name = "question-then-plan"

    def __init__(self) -> None:
        self.plan_calls = 0
        self.agent_histories: list[list[object]] = []

    def decide(self, runtime):
        if runtime.run.mode == "plan":
            self.plan_calls += 1
            if self.plan_calls == 1:
                return AssistantMessage(
                    tool_messages=[
                        ToolMessage(
                            name=REQUEST_USER_INPUT_NAME,
                            call_id="question_1",
                            arguments=question_arguments(),
                        )
                    ]
                )
            return AssistantMessage(
                tool_messages=[
                    ToolMessage(
                        name=REQUEST_PLAN_REVIEW_NAME,
                        call_id="review_1",
                        arguments={"plan": PLAN},
                    )
                ]
            )
        self.agent_histories.append(list(runtime.state.messages))
        return AssistantMessage(content="Implemented.")


class ScriptedPlanPlanner(QuestionThenPlanPlanner):
    def __init__(self, plan_responses: list[AssistantMessage]) -> None:
        super().__init__()
        self.plan_responses = list(plan_responses)

    def decide(self, runtime):
        if runtime.run.mode == "plan":
            return self.plan_responses.pop(0)
        return super().decide(runtime)


PLAN = "# Storage plan\n\n## Summary\nStore the result in PostgreSQL.\n\n## Test Plan\nRun the tests."


def question_call(call_id: str = "question_1") -> ToolMessage:
    return ToolMessage(name=REQUEST_USER_INPUT_NAME, call_id=call_id, arguments=question_arguments())


def review_call(call_id: str = "review_1") -> ToolMessage:
    return ToolMessage(name=REQUEST_PLAN_REVIEW_NAME, call_id=call_id, arguments={"plan": PLAN})


def build_service(tmp_path: Path, planner: QuestionThenPlanPlanner) -> ConversationService:
    runner = AgentRunner(planner, ToolRegistry(tmp_path))
    store = session_store(tmp_path / "store")
    return ConversationService(runner, store)


def test_plan_question_answer_is_saved_once_then_plan_review_starts(tmp_path: Path) -> None:
    planner = QuestionThenPlanPlanner()
    service = build_service(tmp_path, planner)
    request_kinds: list[str] = []

    def interrupt(request):
        request_kinds.append(request.kind)
        if request.kind == "question":
            assert request.questions[0].id == "storage"
            return InterruptDecision("answer", answers={"storage": ["PostgreSQL"]})
        return InterruptDecision("cancel")

    result = service.run_task("Plan the change", mode="plan", interrupt=interrupt)

    assert result.status == "cancelled"
    assert request_kinds == ["question", "plan"]
    assert service.runtime is not None
    assert service.runtime.state.messages[0] == UserMessage(content="Plan the change")
    question_message = service.runtime.state.messages[1]
    assert isinstance(question_message, AssistantMessage)
    assert len(question_message.tool_messages) == 1
    assert question_message.tool_messages[0].status == "succeeded"
    assert json.loads(question_message.tool_messages[0].content or "") == {
        "answers": {"storage": {"answers": ["PostgreSQL"]}}
    }
    review_message = service.runtime.state.messages[2]
    assert isinstance(review_message, AssistantMessage)
    assert "aborted at the user's request" in (review_message.content or "")
    assert review_message.tool_messages[0].name == REQUEST_PLAN_REVIEW_NAME
    assert review_message.tool_messages[0].arguments == {"plan": PLAN}
    assert review_message.tool_messages[0].status == "succeeded"
    assert len([message for message in service.runtime.state.messages if message is question_message]) == 1
    assert [event.kind for event in result.events].count("user_input_requested") == 1
    assert [event.kind for event in result.events].count("user_input_received") == 1


def test_plan_question_cancellation_persists_failed_call_without_plan_review(tmp_path: Path) -> None:
    planner = ScriptedPlanPlanner([AssistantMessage(tool_messages=[question_call()])])
    service = build_service(tmp_path, planner)
    request_kinds: list[str] = []

    result = service.run_task(
        "Plan the change",
        mode="plan",
        interrupt=lambda request: request_kinds.append(request.kind) or InterruptDecision("cancel"),
    )

    assert result.status == "cancelled"
    assert request_kinds == ["question"]
    assert service.runtime is not None
    saved_call = service.runtime.state.messages[-1].tool_messages[0]
    assert saved_call.status == "failed"
    assert saved_call.content == "Plan question cancelled by user."


def test_invalid_question_answers_are_returned_to_model_for_recovery(tmp_path: Path) -> None:
    planner = ScriptedPlanPlanner(
        [
            AssistantMessage(tool_messages=[question_call()]),
            AssistantMessage(tool_messages=[review_call()]),
        ]
    )
    service = build_service(tmp_path, planner)

    def interrupt(request):
        if request.kind == "question":
            return InterruptDecision("answer", answers={})
        return InterruptDecision("cancel")

    result = service.run_task("Plan the change", mode="plan", interrupt=interrupt)

    assert result.status == "cancelled"
    assert service.runtime is not None
    failed = service.runtime.state.messages[1].tool_messages[0]
    assert failed.status == "failed"
    assert failed.retryable is True
    assert "exactly the requested question ids" in (failed.content or "")
    assert service.runtime.state.messages[-1].tool_messages[0].name == REQUEST_PLAN_REVIEW_NAME


def test_request_user_input_must_not_be_mixed_with_execution_tools(tmp_path: Path) -> None:
    planner = ScriptedPlanPlanner(
        [
            AssistantMessage(
                tool_messages=[
                    question_call(),
                    ToolMessage(
                        name="run_command",
                        call_id="command_1",
                        arguments={"command": "Get-ChildItem"},
                    ),
                ]
            ),
            AssistantMessage(tool_messages=[review_call()]),
        ]
    )
    service = build_service(tmp_path, planner)
    request_kinds: list[str] = []

    result = service.run_task(
        "Plan the change",
        mode="plan",
        interrupt=lambda request: request_kinds.append(request.kind) or InterruptDecision("cancel"),
    )

    assert result.status == "cancelled"
    assert request_kinds == ["plan"]
    assert service.runtime is not None
    rejected = service.runtime.state.messages[1]
    assert [tool.status for tool in rejected.tool_messages] == ["failed", "failed"]
    assert all("only tool call" in (tool.content or "") for tool in rejected.tool_messages)
