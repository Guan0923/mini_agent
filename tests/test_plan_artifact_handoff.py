from pathlib import Path

import pytest

from mini_agent.domain import (
    ArtifactMessage,
    AssistantMessage,
    ExecutionPlan,
    StepEvaluation,
    StrategySelection,
    ToolMessage,
    UserMessage,
)
from mini_agent.planning import RuleBasedPlanner
from mini_agent.providers import DeepSeek, ModelConfig
from mini_agent.runtime import AgentRunner, ConversationService, SQLiteSessionStore
from mini_agent.runtime.contracts import InterruptDecision
from mini_agent.storage import FileArtifactStore
from mini_agent.tools import ToolRegistry


class ArtifactPlanner:
    name = "artifact-plan"

    def __init__(self) -> None:
        self.agent_histories: list[list[object]] = []
        self.dynamic_histories: list[list[object]] = []

    def decide(self, runtime):
        if runtime.run.mode == "plan":
            return AssistantMessage(content="1. Write initial.md.")
        self.agent_histories.append(list(runtime.state.messages))
        return AssistantMessage(content="The pending plan remains available.")

    def select_strategy(self, runtime):
        return StrategySelection("reactive", "The manual follow-up only inspects context.")

    def create_dynamic_plan(self, runtime):
        assert runtime.run.input_artifact_ids
        assert isinstance(runtime.state.messages[-2], ArtifactMessage)
        assert runtime.state.messages[-1] == UserMessage(content="Implement the plan")
        self.dynamic_histories.append(list(runtime.state.messages))
        return ExecutionPlan(goal="Implement the reviewed plan.", final_answer="Implemented the artifact plan.")

    def evaluate_step(self, runtime):
        return StepEvaluation("continue", "No executable steps remain.")

    def replan(self, runtime):
        raise AssertionError("The artifact plan should not need replanning.")


def build_service(
    tmp_path: Path,
    planner: ArtifactPlanner | None = None,
    session_store: SQLiteSessionStore | None = None,
) -> ConversationService:
    database = tmp_path / ".mini_agent" / "checkpoints.db"
    runner = AgentRunner(
        planner or ArtifactPlanner(),
        ToolRegistry(tmp_path),
        artifact_store=FileArtifactStore(tmp_path),
    )
    return ConversationService(runner, session_store or SQLiteSessionStore(database))


def test_implement_completes_plan_run_then_creates_agent_run(tmp_path: Path) -> None:
    service = build_service(tmp_path)

    result = service.run_task(
        "Plan the change",
        mode="plan",
        interrupt=lambda request: InterruptDecision("implement"),
    )

    assert result.status == "completed"
    assert result.mode == "agent"
    assert result.task == "Implement the plan"
    assert result.final_answer == "Implemented the artifact plan."
    assert len(result.input_artifact_ids) == 1
    assert service.runtime is not None
    assert service.runtime.state.pending_plan_artifact_id is None
    assert [(summary.mode, summary.status) for summary in service.runtime.state.run_history] == [
        ("plan", "completed"),
        ("agent", "completed"),
    ]
    plan_summary = service.runtime.state.run_history[0]
    assert plan_summary.artifact_ids == result.input_artifact_ids
    artifact = next(message for message in service.runtime.state.messages if isinstance(message, ArtifactMessage))
    assert artifact.artifact_id == result.input_artifact_ids[0]
    assert artifact.relative_path is not None
    assert (tmp_path / artifact.relative_path).read_text(encoding="utf-8") == artifact.content


def test_cancel_preserves_pending_artifact_across_restart_and_manual_agent_turn(tmp_path: Path) -> None:
    planner = ArtifactPlanner()
    service = build_service(tmp_path, planner)
    cancelled = service.run_task(
        "Plan the change",
        mode="plan",
        interrupt=lambda request: InterruptDecision("cancel"),
    )
    assert cancelled.status == "cancelled"
    assert service.active_session is not None
    session_id = service.active_session.session_id
    assert service.runtime is not None
    artifact_id = service.runtime.state.pending_plan_artifact_id
    assert artifact_id is not None

    reopened = ConversationService(
        service.runner,
        SQLiteSessionStore(tmp_path / ".mini_agent" / "checkpoints.db"),
        session_id=session_id,
    )
    assert reopened.runtime is not None
    assert reopened.runtime.state.pending_plan_artifact_id == artifact_id

    result = reopened.run_task("执行", mode="agent")

    assert result.status == "completed"
    assert result.final_answer == "The pending plan remains available."
    assert any(isinstance(message, ArtifactMessage) for message in planner.agent_histories[-1])
    assert reopened.runtime.state.pending_plan_artifact_id == artifact_id


def test_implement_and_clear_session_isolates_agent_context(tmp_path: Path) -> None:
    planner = ArtifactPlanner()
    store = SQLiteSessionStore(tmp_path / ".mini_agent" / "checkpoints.db")
    service = build_service(tmp_path, planner, store)
    source_session = service.new_session("Travel plan")
    assert service.runtime is not None
    service.runtime.state.messages.extend(
        [
            UserMessage(content="Old unrelated request"),
            AssistantMessage(
                content="Old result",
                tool_messages=[
                    ToolMessage(
                        name="list_files",
                        call_id="call_old",
                        content="large old tool result",
                        status="succeeded",
                    )
                ],
            ),
        ]
    )
    service.runtime.save()

    result = service.run_task(
        "Plan the change",
        mode="plan",
        interrupt=lambda request: InterruptDecision("implement_clear_session"),
    )

    assert result.status == "completed"
    assert result.input_artifact_ids
    assert service.active_session is not None
    assert service.active_session.session_id != source_session.session_id
    assert service.active_session.title == "Implement: Travel plan"
    assert service.runtime is not None
    assert [type(message) for message in planner.dynamic_histories[-1]] == [ArtifactMessage, UserMessage]
    assert [type(message) for message in service.runtime.state.messages] == [
        ArtifactMessage,
        UserMessage,
        AssistantMessage,
    ]
    service.runtime.exchange.messages = list(planner.dynamic_histories[-1])
    payload = DeepSeek(ModelConfig("secret", "https://example.test/v1", "demo")).prepare_request(service.runtime)
    assert [message["role"] for message in payload["messages"]] == ["assistant", "user"]
    assert payload["messages"][1]["content"] == "Implement the plan"
    assert "large old tool result" not in str(payload["messages"])
    assert [(summary.mode, summary.status) for summary in service.runtime.state.run_history] == [("agent", "completed")]

    source_state = store.load_runtime(source_session.session_id)
    assert source_state is not None
    assert source_state.pending_plan_artifact_id is None
    assert source_state.current_run is not None
    assert source_state.current_run.handoff is not None
    assert source_state.current_run.handoff.new_session is True
    assert [(summary.mode, summary.status) for summary in source_state.run_history] == [("plan", "completed")]
    source_artifact = next(message for message in source_state.messages if isinstance(message, ArtifactMessage))
    assert source_artifact.artifact_id == result.input_artifact_ids[0]
    assert source_artifact.relative_path is not None
    assert source_session.session_id in source_artifact.relative_path
    assert (tmp_path / source_artifact.relative_path).read_text(encoding="utf-8") == source_artifact.content

    reopened = ConversationService(service.runner, store, session_id=service.active_session.session_id)
    assert reopened.runtime is not None
    assert reopened.runtime.state.current_run is not None
    assert reopened.runtime.state.current_run.input_artifact_ids == result.input_artifact_ids


class FailingSecondSessionStore(SQLiteSessionStore):
    def __init__(self, database: Path) -> None:
        super().__init__(database)
        self.created_sessions = 0

    def create_session(self, title: str | None = None):
        if self.created_sessions == 1:
            raise OSError("session storage unavailable")
        self.created_sessions += 1
        return super().create_session(title)


def test_clear_session_failure_preserves_source_session_and_pending_artifact(tmp_path: Path) -> None:
    store = FailingSecondSessionStore(tmp_path / ".mini_agent" / "checkpoints.db")
    service = build_service(tmp_path, session_store=store)
    source_session = service.new_session("Source plan")

    with pytest.raises(OSError, match="session storage unavailable"):
        service.run_task(
            "Plan the change",
            mode="plan",
            interrupt=lambda request: InterruptDecision("implement_clear_session"),
        )

    assert service.active_session is not None
    assert service.active_session.session_id == source_session.session_id
    assert service.runtime is not None
    artifact_id = service.runtime.state.pending_plan_artifact_id
    assert artifact_id is not None
    persisted = store.load_runtime(source_session.session_id)
    assert persisted is not None
    assert persisted.pending_plan_artifact_id == artifact_id
    assert len(store.list_sessions()) == 1


def test_deepseek_maps_materialized_artifact_to_assistant_wire_message(tmp_path: Path) -> None:
    runtime = AgentRunner(RuleBasedPlanner(), ToolRegistry()).new_runtime(task="Implement it")
    artifact = FileArtifactStore(tmp_path).create_plan(
        runtime.state.session_id,
        runtime.run.run_id,
        1,
        "# Plan\n\nImplement the change.",
    )
    runtime.exchange.messages = [UserMessage(content="Make a plan"), artifact, UserMessage(content="执行")]

    payload = DeepSeek(ModelConfig("secret", "https://example.test/v1", "demo")).prepare_request(runtime)

    assert payload["messages"][1] == {
        "role": "assistant",
        "content": (
            f"[Artifact kind=plan id={artifact.artifact_id} revision=1 path={artifact.relative_path}]\n"
            "# Plan\n\nImplement the change."
        ),
    }


class FailingArtifactStore:
    def create_plan(self, session_id: str, run_id: str, revision: int, content: str) -> ArtifactMessage:
        raise OSError("disk unavailable")


def test_plan_fails_before_review_when_artifact_cannot_be_persisted() -> None:
    called = False

    def interrupt(request):
        nonlocal called
        called = True
        return InterruptDecision("implement")

    runner = AgentRunner(ArtifactPlanner(), ToolRegistry(), artifact_store=FailingArtifactStore())
    runtime = runner.new_runtime(task="Plan the change", mode="plan", interrupt=interrupt)

    result = runner.run(runtime)

    assert result.status == "failed"
    assert result.final_answer == "Unable to persist plan artifact: disk unavailable"
    assert called is False
