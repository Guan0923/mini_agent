from pathlib import Path

import pytest

from backend.domain import (
    AssistantMessage,
    StrategySelection,
    ToolMessage,
    UserMessage,
    message_from_dict,
)
from backend.runtime import AgentRunner, ConversationService, SQLiteSessionStore
from backend.runtime.core.contracts import InterruptDecision
from backend.runtime.planning.review import REQUEST_PLAN_REVIEW_NAME
from backend.tools import ToolRegistry

PLAN = "# Reviewed change\n\n## Summary\nImplement the reviewed change."


def review_message(call_id: str = "review_1") -> AssistantMessage:
    return AssistantMessage(
        tool_messages=[
            ToolMessage(
                name=REQUEST_PLAN_REVIEW_NAME,
                call_id=call_id,
                arguments={"plan": PLAN},
            )
        ]
    )


def completed_review_message(call_id: str = "review_1") -> AssistantMessage:
    return AssistantMessage(
        tool_messages=[
            ToolMessage(
                name=REQUEST_PLAN_REVIEW_NAME,
                call_id=call_id,
                arguments={"plan": PLAN},
                status="succeeded",
                content="Plan submitted for review.",
                retryable=False,
            )
        ]
    )


class PlanMessagePlanner:
    name = "plan-message"

    def __init__(self) -> None:
        self.agent_histories: list[list[object]] = []

    def decide(self, runtime):
        if runtime.run.mode == "plan":
            return review_message()
        self.agent_histories.append(list(runtime.state.messages))
        return AssistantMessage(content="Implemented from ordinary messages.")

    def select_strategy(self, runtime):
        return StrategySelection("reactive", "The model can execute directly from conversation history.")


class ConversationPlanner(PlanMessagePlanner):
    def decide(self, runtime):
        if runtime.run.mode == "plan":
            return AssistantMessage(content="Hello! What would you like to discuss?")
        return super().decide(runtime)


def build_service(tmp_path: Path, planner: PlanMessagePlanner) -> ConversationService:
    runner = AgentRunner(planner, ToolRegistry(tmp_path))
    store = SQLiteSessionStore(tmp_path / ".mini_agent" / "checkpoints.db")
    return ConversationService(runner, store)


def test_plan_implement_keeps_control_call_as_ordinary_history(tmp_path: Path) -> None:
    planner = PlanMessagePlanner()
    service = build_service(tmp_path, planner)

    result = service.run_task(
        "Plan the change",
        mode="plan",
        interrupt=lambda _request: InterruptDecision("implement"),
    )

    assert result.mode == "agent"
    assert result.strategy == "reactive"
    assert planner.agent_histories[-1] == [
        UserMessage(content="Plan the change"),
        completed_review_message(),
        UserMessage(content="Implement the plan"),
    ]
    assert all(message.role != "artifact" for message in service.runtime.state.messages)


def test_plan_implement_clear_session_seeds_only_final_plan(tmp_path: Path) -> None:
    planner = PlanMessagePlanner()
    service = build_service(tmp_path, planner)
    source = service.new_session("Source conversation")
    assert service.runtime is not None
    service.runtime.state.messages.extend([UserMessage(content="Old request"), AssistantMessage(content="Old answer")])
    service.runtime.save()

    result = service.run_task(
        "Plan the change",
        mode="plan",
        interrupt=lambda _request: InterruptDecision("implement_clear_session"),
    )

    assert result.mode == "agent"
    assert service.active_session is not None
    assert service.active_session.session_id != source.session_id
    assert planner.agent_histories[-1] == [
        AssistantMessage(content=PLAN),
        UserMessage(content="Implement the plan"),
    ]


def test_legacy_artifact_message_is_not_loadable() -> None:
    with pytest.raises(ValueError, match="Unsupported message role: artifact"):
        message_from_dict(
            {
                "role": "artifact",
                "artifact_id": "artifact_old",
                "content": "old plan",
                "sha256": "hash",
                "revision": 1,
                "created_by_run_id": "run_old",
            }
        )


def test_plan_conversation_completes_without_review_or_format_repair(tmp_path: Path) -> None:
    planner = ConversationPlanner()
    service = build_service(tmp_path, planner)
    events = []

    result = service.run_task(
        "Hello",
        mode="plan",
        on_event=events.append,
        interrupt=lambda _request: pytest.fail("ordinary conversation must not interrupt"),
    )

    assert result.status == "completed"
    assert result.final_answer == "Hello! What would you like to discuss?"
    assert service.runtime is not None
    assert service.runtime.state.messages == [
        UserMessage(content="Hello"),
        AssistantMessage(content="Hello! What would you like to discuss?"),
    ]
    assert [event.kind for event in events].count("response") == 1
    assert all(event.kind != "plan" for event in events)
    assert all(event.kind != "model_repair" for event in events)


def test_cancelled_plan_history_survives_restart(tmp_path: Path) -> None:
    planner = PlanMessagePlanner()
    service = build_service(tmp_path, planner)
    result = service.run_task(
        "Plan the change",
        mode="plan",
        interrupt=lambda _request: InterruptDecision("cancel"),
    )
    assert result.status == "cancelled"
    assert service.active_session is not None

    reopened = ConversationService(
        service.runner,
        SQLiteSessionStore(tmp_path / ".mini_agent" / "checkpoints.db"),
        session_id=service.active_session.session_id,
    )

    assert reopened.runtime is not None
    assert reopened.runtime.state.messages == [
        UserMessage(content="Plan the change"),
        completed_review_message(),
    ]


class FailingSecondSessionStore(SQLiteSessionStore):
    def __init__(self, database: Path) -> None:
        super().__init__(database)
        self.created_sessions = 0

    def create_session(self, title: str | None = None):
        if self.created_sessions == 1:
            raise OSError("session storage unavailable")
        self.created_sessions += 1
        return super().create_session(title)


def test_clear_session_failure_preserves_source_plan_history(tmp_path: Path) -> None:
    planner = PlanMessagePlanner()
    store = FailingSecondSessionStore(tmp_path / ".mini_agent" / "checkpoints.db")
    service = ConversationService(AgentRunner(planner, ToolRegistry(tmp_path)), store)
    source = service.new_session("Source plan")

    with pytest.raises(OSError, match="session storage unavailable"):
        service.run_task(
            "Plan the change",
            mode="plan",
            interrupt=lambda _request: InterruptDecision("implement_clear_session"),
        )

    assert service.active_session is not None
    assert service.active_session.session_id == source.session_id
    persisted = store.load_runtime(source.session_id)
    assert persisted is not None
    assert persisted.messages[-1] == completed_review_message()
    assert len(store.list_sessions()) == 1
