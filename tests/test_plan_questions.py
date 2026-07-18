import json
from pathlib import Path

import pytest

from mini_agent.domain import AssistantMessage, StrategySelection, ToolMessage, UserMessage
from mini_agent.runtime import AgentRunner, ConversationService, SQLiteSessionStore
from mini_agent.runtime.conversation.user_input import (
    REQUEST_USER_INPUT_NAME,
    format_user_input_answers,
    parse_user_input_questions,
    validate_user_input_answers,
)
from mini_agent.runtime.core.contracts import InterruptDecision, QuestionOption, UserQuestion
from mini_agent.runtime.planning.review import REQUEST_PLAN_REVIEW_NAME
from mini_agent.tools import ToolRegistry


def question_arguments() -> dict[str, object]:
    return {
        "questions": [
            {
                "id": "storage",
                "header": "Storage",
                "question": "Where should the result be stored?",
                "options": [
                    {"label": "SQLite", "description": "Keep the data in the existing database."},
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
                QuestionOption("SQLite", "Keep the data in the existing database."),
                QuestionOption("JSONL", "Write the data to the existing audit stream."),
            ),
        ),
    )


def test_request_user_input_parser_rejects_duplicate_question_ids() -> None:
    arguments = question_arguments()
    arguments["questions"] = [*arguments["questions"], arguments["questions"][0]]

    with pytest.raises(ValueError, match="unique"):
        parse_user_input_questions(arguments)


def test_request_user_input_parser_accepts_more_than_three_options() -> None:
    arguments = question_arguments()
    arguments["questions"][0]["options"].extend(
        [
            {"label": "Text", "description": "Use a text file."},
            {"label": "Memory", "description": "Keep the result in memory."},
        ]
    )

    questions = parse_user_input_questions(arguments)

    assert [option.label for option in questions[0].options] == ["SQLite", "JSONL", "Text", "Memory"]


@pytest.mark.parametrize("option_count", [0, 1])
def test_request_user_input_parser_accepts_fewer_than_two_options(option_count: int) -> None:
    arguments = question_arguments()
    arguments["questions"][0]["options"] = arguments["questions"][0]["options"][:option_count]

    questions = parse_user_input_questions(arguments)

    assert len(questions[0].options) == option_count


def test_request_user_input_parser_accepts_more_than_three_questions() -> None:
    arguments = question_arguments()
    template = arguments["questions"][0]
    arguments["questions"] = [
        {**template, "id": f"question_{index}"}
        for index in range(4)
    ]

    questions = parse_user_input_questions(arguments)

    assert [question.id for question in questions] == [f"question_{index}" for index in range(4)]


def test_request_user_input_parser_rejects_empty_questions() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        parse_user_input_questions({"questions": []})


def test_request_user_input_parser_rejects_whitespace_only_text() -> None:
    arguments = question_arguments()
    arguments["questions"][0]["options"][0]["label"] = "   "

    with pytest.raises(ValueError, match="must not be blank"):
        parse_user_input_questions(arguments)


def test_request_user_input_parser_filters_exact_client_other_label() -> None:
    arguments = question_arguments()
    arguments["questions"][0]["options"].insert(
        0,
        {"label": "  其他  ", "description": "Provide a custom answer."},
    )

    questions = parse_user_input_questions(arguments)

    assert [option.label for option in questions[0].options] == ["SQLite", "JSONL"]


@pytest.mark.parametrize("label", ["以上都不对", "none of the above"])
def test_request_user_input_parser_keeps_semantically_similar_other_labels(label: str) -> None:
    arguments = question_arguments()
    arguments["questions"][0]["options"][0]["label"] = label

    questions = parse_user_input_questions(arguments)

    assert questions[0].options[0].label == label


def test_user_input_answers_use_codex_style_tool_result_shape() -> None:
    result = format_user_input_answers({"storage": ["SQLite"]})

    assert json.loads(result) == {"answers": {"storage": {"answers": ["SQLite"]}}}



def test_user_input_answers_allow_explicit_skips() -> None:
    questions = parse_user_input_questions(question_arguments())

    answers = validate_user_input_answers(questions, {"storage": []})

    assert answers == {"storage": []}
    assert json.loads(format_user_input_answers(answers)) == {
        "answers": {"storage": {"answers": []}}
    }


@pytest.mark.parametrize("value", [[""], ["one", "two"]])
def test_user_input_answers_reject_invalid_non_skip_values(value: list[str]) -> None:
    questions = parse_user_input_questions(question_arguments())

    with pytest.raises(ValueError):
        validate_user_input_answers(questions, {"storage": value})

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

    def select_strategy(self, runtime):
        return StrategySelection("reactive", "Execute from the approved conversation history.")


class ScriptedPlanPlanner(QuestionThenPlanPlanner):
    def __init__(self, plan_responses: list[AssistantMessage]) -> None:
        super().__init__()
        self.plan_responses = list(plan_responses)

    def decide(self, runtime):
        if runtime.run.mode == "plan":
            return self.plan_responses.pop(0)
        return super().decide(runtime)


PLAN = "# Storage plan\n\n## Summary\nStore the result in SQLite.\n\n## Test Plan\nRun the tests."


def question_call(call_id: str = "question_1") -> ToolMessage:
    return ToolMessage(name=REQUEST_USER_INPUT_NAME, call_id=call_id, arguments=question_arguments())


def review_call(call_id: str = "review_1") -> ToolMessage:
    return ToolMessage(name=REQUEST_PLAN_REVIEW_NAME, call_id=call_id, arguments={"plan": PLAN})


def build_service(tmp_path: Path, planner: QuestionThenPlanPlanner) -> ConversationService:
    runner = AgentRunner(planner, ToolRegistry(tmp_path))
    store = SQLiteSessionStore(tmp_path / ".mini_agent" / "checkpoints.db")
    return ConversationService(runner, store)


def test_plan_question_answer_is_saved_once_then_plan_review_starts(tmp_path: Path) -> None:
    planner = QuestionThenPlanPlanner()
    service = build_service(tmp_path, planner)
    request_kinds: list[str] = []

    def interrupt(request):
        request_kinds.append(request.kind)
        if request.kind == "question":
            assert request.questions[0].id == "storage"
            return InterruptDecision("answer", answers={"storage": ["SQLite"]})
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
        "answers": {"storage": {"answers": ["SQLite"]}}
    }
    review_message = service.runtime.state.messages[2]
    assert isinstance(review_message, AssistantMessage)
    assert review_message.content is None
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


def test_default_interrupt_cancels_and_persists_plan_question(tmp_path: Path) -> None:
    planner = ScriptedPlanPlanner([AssistantMessage(tool_messages=[question_call()])])
    runner = AgentRunner(planner, ToolRegistry(tmp_path))
    runtime = runner.new_runtime(task="Plan the change", mode="plan")

    result = runner.run(runtime)

    assert result.status == "cancelled"
    assert runtime.state.messages[-1].tool_messages[0].status == "failed"
    assert runtime.state.messages[-1].tool_messages[0].content == "Plan question cancelled by user."


def test_repeated_invalid_question_calls_stop_at_recovery_limit(tmp_path: Path) -> None:
    invalid = question_arguments()
    invalid["questions"] = []
    planner = ScriptedPlanPlanner(
        [
            AssistantMessage(
                tool_messages=[
                    ToolMessage(name=REQUEST_USER_INPUT_NAME, call_id=f"question_{index}", arguments=invalid)
                ]
            )
            for index in range(3)
        ]
    )
    service = build_service(tmp_path, planner)

    result = service.run_task("Plan the change", mode="plan", interrupt=lambda _request: pytest.fail())

    assert result.status == "failed"
    assert result.final_answer == "Stopped after repeated invalid request_user_input calls."
    assert len(result.actions) == 3


def test_same_session_implement_replays_question_tool_history(tmp_path: Path) -> None:
    planner = QuestionThenPlanPlanner()
    service = build_service(tmp_path, planner)

    def interrupt(request):
        if request.kind == "question":
            return InterruptDecision("answer", answers={"storage": ["SQLite"]})
        return InterruptDecision("implement")

    result = service.run_task("Plan the change", mode="plan", interrupt=interrupt)

    assert result.mode == "agent"
    history = planner.agent_histories[-1]
    assert isinstance(history[1], AssistantMessage)
    assert history[1].tool_messages[0].name == REQUEST_USER_INPUT_NAME
    assert history[1].tool_messages[0].status == "succeeded"
    assert history[-2].tool_messages[0].name == REQUEST_PLAN_REVIEW_NAME
    assert history[-2].tool_messages[0].arguments == {"plan": PLAN}
    assert history[-2].tool_messages[0].status == "succeeded"
    assert history[-1] == UserMessage(content="Implement the plan")


def test_clear_session_implement_drops_question_tool_history(tmp_path: Path) -> None:
    planner = QuestionThenPlanPlanner()
    service = build_service(tmp_path, planner)

    def interrupt(request):
        if request.kind == "question":
            return InterruptDecision("answer", answers={"storage": ["SQLite"]})
        return InterruptDecision("implement_clear_session")

    result = service.run_task("Plan the change", mode="plan", interrupt=interrupt)

    assert result.mode == "agent"
    assert planner.agent_histories[-1] == [
        AssistantMessage(content=PLAN),
        UserMessage(content="Implement the plan"),
    ]
