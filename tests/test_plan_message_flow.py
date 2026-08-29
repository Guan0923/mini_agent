from pathlib import Path

import pytest

from backend.domain import (
    AssistantMessage,
    ToolMessage,
    UserMessage,
    message_from_dict,
)
from backend.planning.context_management import ContextCompactionResult
from backend.runtime import AgentRunner, ConversationService
from backend.runtime.core.contracts import InterruptDecision
from backend.runtime.core.events import RuntimeEvent
from backend.runtime.planning.review import REQUEST_PLAN_REVIEW_NAME
from backend.tools import ToolRegistry
from tests.local_store import session_store

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

    def compact_context(self, runtime):
        assert runtime.services.publish is not None
        runtime.services.publish(
            RuntimeEvent("context_compaction_completed", data={"summary": "Compacted reviewed plan."})
        )
        return ContextCompactionResult(True, 2, 1, "Compacted reviewed plan.")


class ConversationPlanner(PlanMessagePlanner):
    def decide(self, runtime):
        if runtime.run.mode == "plan":
            return AssistantMessage(content="Hello! What would you like to discuss?")
        return super().decide(runtime)


def build_service(tmp_path: Path, planner: PlanMessagePlanner) -> ConversationService:
    runner = AgentRunner(planner, ToolRegistry(tmp_path))
    store = session_store(tmp_path / "store")
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
    assert planner.agent_histories[-1] == [
        UserMessage(content="Plan the change"),
        completed_review_message(),
        UserMessage(content=PLAN),
    ]
    assert all(message.role != "artifact" for message in service.runtime.state.messages)


def test_plan_implement_after_compaction_keeps_session_and_uses_raw_plan(tmp_path: Path) -> None:
    planner = PlanMessagePlanner()
    service = build_service(tmp_path, planner)
    source = service.new_session("Source conversation")
    assert service.runtime is not None
    service.runtime.state.messages.extend([UserMessage(content="Old request"), AssistantMessage(content="Old answer")])
    service.runtime.save()

    result = service.run_task(
        "Plan the change",
        mode="plan",
        interrupt=lambda _request: InterruptDecision("implement_and_compaction"),
    )

    assert result.mode == "agent"
    assert service.active_session is not None
    assert service.active_session.session_id == source.session_id
    assert planner.agent_histories[-1][-1] == UserMessage(content=PLAN)


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


def test_stay_in_plan_mode_history_survives_restart(tmp_path: Path) -> None:
    planner = PlanMessagePlanner()
    service = build_service(tmp_path, planner)
    result = service.run_task(
        "Plan the change",
        mode="plan",
        interrupt=lambda _request: InterruptDecision("stay_in_plan_mode"),
    )
    assert result.status == "completed"
    assert result.mode == "plan"
    assert service.active_session is not None

    reopened = ConversationService(
        service.runner,
        session_store(tmp_path / "store"),
        session_id=service.active_session.session_id,
    )

    assert reopened.runtime is not None
    assert reopened.runtime.state.messages[0] == UserMessage(content="Plan the change")
    assert reopened.runtime.state.messages[1].tool_messages == completed_review_message().tool_messages
    assert reopened.runtime.state.messages[1].content is None
