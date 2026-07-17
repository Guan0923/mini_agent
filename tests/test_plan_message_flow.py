from pathlib import Path

import pytest

from mini_agent.domain import (
    ArtifactMessage,
    AssistantMessage,
    UserMessage,
    message_from_dict,
    message_to_dict,
)
from mini_agent.runtime import AgentRunner, ConversationService, SQLiteSessionStore
from mini_agent.runtime.contracts import InterruptDecision
from mini_agent.tools import ToolRegistry


class PlanMessagePlanner:
    name = "plan-message"

    def __init__(self) -> None:
        self.agent_histories: list[list[object]] = []

    def decide(self, runtime):
        if runtime.run.mode == "plan":
            return AssistantMessage(content="1. Implement the reviewed change.")
        self.agent_histories.append(list(runtime.state.messages))
        return AssistantMessage(content="Implemented from ordinary messages.")


class FormatRepairPlanner(PlanMessagePlanner):
    def __init__(self) -> None:
        super().__init__()
        self.plan_responses = 0

    def decide(self, runtime):
        if runtime.run.mode == "plan":
            self.plan_responses += 1
            if self.plan_responses == 1:
                return AssistantMessage(content="Inspect the project, then implement the change.")
            return AssistantMessage(content="1. Inspect the project.\n2. Implement the change.")
        return super().decide(runtime)


def build_service(tmp_path: Path, planner: PlanMessagePlanner) -> ConversationService:
    runner = AgentRunner(planner, ToolRegistry(tmp_path))
    store = SQLiteSessionStore(tmp_path / ".mini_agent" / "checkpoints.db")
    return ConversationService(runner, store)


def test_plan_implement_keeps_complete_history_as_ordinary_messages(tmp_path: Path) -> None:
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
        AssistantMessage(content="1. Implement the reviewed change."),
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
        AssistantMessage(content="1. Implement the reviewed change."),
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


def test_artifact_message_is_not_serializable_as_chat_history() -> None:
    artifact = ArtifactMessage(
        artifact_id="artifact_dormant",
        content="Dormant snapshot",
        sha256="hash",
        revision=1,
        created_by_run_id="run_old",
    )

    with pytest.raises(TypeError, match="ArtifactMessage is not an active chat message"):
        message_to_dict(artifact)


def test_plan_format_repair_preserves_all_messages_and_records_final_plan_once(tmp_path: Path) -> None:
    planner = FormatRepairPlanner()
    service = build_service(tmp_path, planner)

    result = service.run_task(
        "Plan the change",
        mode="plan",
        interrupt=lambda _request: InterruptDecision("cancel"),
    )

    assert result.status == "cancelled"
    assert service.runtime is not None
    assert service.runtime.state.messages == [
        UserMessage(content="Plan the change"),
        AssistantMessage(content="Inspect the project, then implement the change."),
        UserMessage(
            content=(
                "[Plan format correction]\nUse request_user_input for material clarification questions; "
                "otherwise return a concise numbered plan starting with 1."
            )
        ),
        AssistantMessage(content="1. Inspect the project.\n2. Implement the change."),
    ]


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
        AssistantMessage(content="1. Implement the reviewed change."),
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
    assert persisted.messages[-1] == AssistantMessage(content="1. Implement the reviewed change.")
    assert len(store.list_sessions()) == 1
