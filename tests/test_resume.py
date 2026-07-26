import json
import os
from pathlib import Path

import psycopg
import pytest

from backend.domain import (
    AgentAction,
    AssistantMessage,
    ExecutionPlan,
    PlanStep,
    RecoveryCheckpoint,
    RunProvenance,
    RunState,
    StepEvaluation,
    ToolMessage,
)
from backend.runtime import AgentRunner, ConversationService, PostgresSessionStore, RuntimeEvent
from backend.runtime.conversation.recovery import reconstruct_attempt
from backend.runtime.core.context import RuntimeState
from backend.runtime.core.contracts import InterruptDecision
from backend.runtime.planning.review import REQUEST_PLAN_REVIEW_NAME
from backend.tools import Tool, ToolRegistry


class InterruptingToolPlanner:
    name = "interrupting-tool"

    def decide(self, runtime):
        if any(
            tool.status == "indeterminate"
            for message in runtime.state.messages
            if isinstance(message, AssistantMessage)
            for tool in message.tool_messages
        ):
            return AssistantMessage(content="Recovered without replaying the tool.")
        return AssistantMessage(
            tool_messages=[ToolMessage(name="side_effect", call_id="call_side_effect", arguments={})]
        )


class FinalPlanner:
    name = "final"

    def decide(self, runtime):
        return AssistantMessage(content="Resumed.")


class ResumeDynamicPlanner:
    name = "resume-dynamic"

    def __init__(self) -> None:
        self.replan_reasons: list[str] = []
        self.replan_statuses: list[str] = []

    def create_plan(self, runtime) -> ExecutionPlan:
        return ExecutionPlan(
            goal="Perform one side effect safely.",
            steps=[
                PlanStep(
                    id="effect",
                    description="Create the effect once",
                    action=AgentAction(type="tool_call", tool="side_effect", arguments={}),
                )
            ],
        )

    def evaluate_step(self, runtime) -> StepEvaluation:
        return StepEvaluation("continue", "The step completed.")

    def replan(self, runtime) -> ExecutionPlan:
        plan = runtime.exchange.context["plan"]
        reason = runtime.exchange.context["reason"]
        assert plan.steps[0].status in {"failed", "indeterminate"}
        self.replan_reasons.append(reason)
        self.replan_statuses.append(plan.steps[0].status)
        return ExecutionPlan(goal="Inspect instead of replaying.", steps=[], final_answer="Recovered safely.")


class PlanHandoffPlanner:
    name = "plan-handoff"

    def decide(self, runtime):
        if runtime.run.mode == "plan":
            return AssistantMessage(
                tool_messages=[
                    ToolMessage(
                        name=REQUEST_PLAN_REVIEW_NAME,
                        call_id="review_1",
                        arguments={"plan": "Implement the reviewed change."},
                    )
                ]
            )
        return AssistantMessage(content="Implemented from the reviewed plan.")


def shared_service(tmp_path: Path, planner, tools: ToolRegistry) -> tuple[ConversationService, PostgresSessionStore]:
    store = PostgresSessionStore()
    runner = AgentRunner(
        planner,
        tools,
        strategy="reactive",
        checkpoints=store,
        workspace_root=str(tmp_path.resolve()),
    )
    return ConversationService(runner, store), store


def test_resume_creates_linked_attempt_without_replaying_indeterminate_tool(tmp_path: Path) -> None:
    effect = tmp_path / "effect.txt"

    def interrupt_after_effect() -> str:
        effect.write_text(effect.read_text() + "x" if effect.exists() else "x")
        raise KeyboardInterrupt

    tools = ToolRegistry([Tool("side_effect", "Create one side effect.", interrupt_after_effect)])
    service, store = shared_service(tmp_path, InterruptingToolPlanner(), tools)

    with pytest.raises(KeyboardInterrupt):
        service.run_task("perform once", mode="agent")

    assert service.active_session is not None
    session_id = service.active_session.session_id
    preview = service.prepare_resume(session_id)
    source_run_id = preview.run_id
    assert preview.status == "failed"
    assert preview.stop_reason == "process_interrupted"
    assert preview.indeterminate_call_ids == ("call_side_effect",)

    reopened = ConversationService(service.runner, store)
    result = reopened.resume_session(
        session_id,
        interrupt=lambda _request: InterruptDecision("continue"),
    )

    assert result is not None and result.status == "completed"
    assert result.run_id != source_run_id
    assert result.provenance.source_run_id == source_run_id
    assert result.provenance.attempt == 2
    assert effect.read_text() == "x"
    indeterminate = [
        tool for message in result.history if isinstance(message, AssistantMessage) for tool in message.tool_messages
    ]
    assert [(tool.call_id, tool.status) for tool in indeterminate] == [("call_side_effect", "indeterminate")]
    source_messages = store.load_runtime_messages(session_id, source_run_id)
    assert {message.kind for message in source_messages} >= {"run_interrupted", "tool_indeterminate"}
    indeterminate_event = next(message for message in source_messages if message.kind == "tool_indeterminate")
    assert indeterminate_event.data["session_id"] == session_id
    assert indeterminate_event.data["workspace_root"] == str(tmp_path.resolve())
    assert indeterminate_event.data["workflow_id"] == result.provenance.workflow_id
    archived = store.load_runtime(session_id)
    assert archived is not None
    assert any(summary.run_id == source_run_id for summary in archived.run_history)


def test_cooperative_pause_is_cancelled_resumable_and_preserves_workflow_identity(tmp_path: Path) -> None:
    service, store = shared_service(tmp_path, FinalPlanner(), ToolRegistry())

    paused = service.run_task("pause me", mode="agent", suspend_requested=lambda: True)

    assert paused.status == "cancelled"
    assert paused.stop_reason == "user_paused"
    assert service.active_session is not None
    workflow_id = paused.provenance.workflow_id
    preview = service.prepare_resume(service.active_session.session_id)
    assert preview.status == "cancelled"
    assert preview.stop_reason == "user_paused"

    reopened = ConversationService(service.runner, store)
    resumed = reopened.resume_session(
        preview.session_id,
        interrupt=lambda _request: InterruptDecision("continue"),
    )

    assert resumed is not None and resumed.status == "completed"
    assert resumed.provenance.workflow_id == workflow_id
    assert resumed.provenance.attempt == 2
    with psycopg.connect(os.environ["TEST_DATABASE_URL"]) as connection:
        transition_reason = connection.execute(
            "SELECT reason FROM checkpoints WHERE run_id = %s ORDER BY id DESC LIMIT 1",
            (paused.run_id,),
        ).fetchone()[0]
        attempt_starts = connection.execute(
            "SELECT started_at FROM session_runs WHERE workflow_id = %s ORDER BY attempt",
            (workflow_id,),
        ).fetchall()
    assert transition_reason == "run_cancelled"
    assert len(attempt_starts) == 2
    assert attempt_starts[0][0] != attempt_starts[1][0]


def test_dynamic_resume_replans_indeterminate_step_without_replaying_tool(tmp_path: Path) -> None:
    effect = tmp_path / "dynamic-effect.txt"

    def interrupt_after_effect() -> str:
        effect.write_text(effect.read_text() + "x" if effect.exists() else "x")
        raise KeyboardInterrupt

    planner = ResumeDynamicPlanner()
    tools = ToolRegistry([Tool("side_effect", "Create one side effect.", interrupt_after_effect)])
    store = PostgresSessionStore()
    runner = AgentRunner(
        planner,
        tools,
        strategy="dynamic_replan",
        checkpoints=store,
        workspace_root=str(tmp_path.resolve()),
    )
    service = ConversationService(runner, store)

    with pytest.raises(KeyboardInterrupt):
        service.run_task("perform dynamic work", mode="agent")
    assert service.active_session is not None

    resumed = ConversationService(runner, store).resume_session(
        service.active_session.session_id,
        interrupt=lambda _request: InterruptDecision("continue"),
    )

    assert resumed is not None and resumed.status == "completed"
    assert effect.read_text() == "x"
    assert planner.replan_reasons and "indeterminate" in planner.replan_reasons[0]
    assert planner.replan_statuses == ["indeterminate"]
    assert resumed.plan_history[-1].steps[0].status == "indeterminate"


def test_dynamic_resume_replans_step_that_never_reached_tool_call(tmp_path: Path) -> None:
    effect = tmp_path / "not-called.txt"

    def side_effect() -> str:
        effect.write_text("called")
        return "called"

    planner = ResumeDynamicPlanner()
    tools = ToolRegistry([Tool("side_effect", "Create one side effect.", side_effect)])
    store = PostgresSessionStore()
    runner = AgentRunner(
        planner,
        tools,
        strategy="dynamic_replan",
        checkpoints=store,
        workspace_root=str(tmp_path.resolve()),
    )
    service = ConversationService(runner, store)

    def interrupt_after_step_started(event: RuntimeEvent) -> None:
        if event.kind == "plan_progress" and event.data.get("trigger") == "step_started":
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        service.run_task("stop before the tool call", mode="agent", on_event=interrupt_after_step_started)
    assert service.active_session is not None
    preview = service.prepare_resume(service.active_session.session_id)
    assert preview.indeterminate_call_ids == ()
    assert not effect.exists()

    resumed = ConversationService(runner, store).resume_session(
        service.active_session.session_id,
        interrupt=lambda _request: InterruptDecision("continue"),
    )

    assert resumed is not None and resumed.status == "completed"
    assert not effect.exists()
    assert planner.replan_statuses == ["failed"]
    assert "fresh decision" in planner.replan_reasons[0]


def test_plan_review_handoff_creates_new_workflow_with_parent_source(tmp_path: Path) -> None:
    service, _store = shared_service(tmp_path, PlanHandoffPlanner(), ToolRegistry())

    result = service.run_task(
        "plan the change",
        mode="plan",
        interrupt=lambda _request: InterruptDecision("implement"),
    )

    assert result.status == "completed"
    assert result.provenance.trigger == "handoff"
    assert service.active_session is not None
    assert result.provenance.source_session_id == service.active_session.session_id
    assert result.provenance.source_run_id is not None
    assert result.provenance.source_run_id != result.run_id
    assert service.runtime is not None
    source = next(
        summary for summary in service.runtime.state.run_history if summary.run_id == result.provenance.source_run_id
    )
    assert source.workflow_id != result.provenance.workflow_id


def test_legacy_run_state_defaults_provenance_to_original_run_id() -> None:
    state = RunState(task="legacy", mode="agent")
    payload = state.to_dict()
    payload.pop("provenance")
    payload.pop("checkpoint")

    restored = RunState.from_dict(payload)

    assert restored.provenance.workflow_id == state.run_id
    assert restored.provenance.trigger == "legacy"


def test_running_state_is_archived_as_failed_after_process_interruption() -> None:
    state = RuntimeState(
        session_id="session_1",
        status="running",
        current_run=RunState(task="resume me", mode="agent", status="running"),
    )

    source, resumed = reconstruct_attempt(state)

    assert source.current_run is not None and source.current_run.checkpoint is not None
    assert source.current_run.status == "failed"
    assert source.current_run.stop_reason == "process_interrupted"
    assert source.current_run.checkpoint.interruption == "process_interrupted"
    assert resumed.current_run is not None
    assert resumed.current_run.run_id != source.current_run.run_id
    assert resumed.current_run.provenance.attempt == source.current_run.provenance.attempt + 1
    assert resumed.current_run.checkpoint is not None
    assert resumed.current_run.checkpoint.reason == "run_resumed"


def test_failed_and_cancelled_runs_are_resumable() -> None:
    for status, reason in (("failed", "execution_failed"), ("cancelled", "user_cancelled")):
        state = RuntimeState(
            session_id=f"session_{status}",
            status="idle",
            current_run=RunState(task="resume me", mode="agent", status=status, stop_reason=reason),
        )

        source, resumed = reconstruct_attempt(state)

        assert source.current_run is not None and source.current_run.status == status
        assert source.current_run.stop_reason == reason
        assert resumed.current_run is not None and resumed.current_run.status == "running"
        assert resumed.current_run.provenance.attempt == 2


def test_legacy_statuses_migrate_to_four_state_model() -> None:
    expected = {
        "suspended": ("cancelled", "user_paused"),
        "interrupted": ("failed", "process_interrupted"),
        "terminated": ("cancelled", "user_terminated"),
    }
    for legacy_status, (status, reason) in expected.items():
        restored = RunState.from_dict(
            {"task": "legacy", "mode": "agent", "run_id": "run_legacy", "status": legacy_status}
        )

        assert restored.status == status
        assert restored.stop_reason == reason


def test_provenance_and_recovery_checkpoint_round_trip() -> None:
    state = RunState(
        task="trace me",
        mode="agent",
        provenance=RunProvenance(
            workflow_id="workflow_1",
            attempt=3,
            trigger="resume",
            workspace_root="C:/workspace",
            source_session_id="session_parent",
            source_run_id="run_parent",
        ),
        checkpoint=RecoveryCheckpoint(
            reason="tool_call",
            timestamp="2026-07-23T00:00:00+00:00",
            call_id="call_1",
            exchange_id="exchange_1",
            interruption="process_interrupted",
            indeterminate_call_ids=("call_1",),
        ),
    )

    restored = RunState.from_dict(state.to_dict())

    assert restored.provenance == state.provenance
    assert restored.checkpoint == state.checkpoint


def test_resume_back_leaves_cancelled_session_unchanged(tmp_path: Path) -> None:
    service, store = shared_service(tmp_path, FinalPlanner(), ToolRegistry())
    service.run_task("pause me", mode="agent", suspend_requested=lambda: True)
    assert service.active_session is not None
    session_id = service.active_session.session_id

    reopened = ConversationService(service.runner, store)
    result = reopened.resume_session(
        session_id,
        interrupt=lambda _request: InterruptDecision("back"),
    )

    assert result is None
    assert reopened.active_session is None

    restored = store.load_runtime(session_id)
    assert restored is not None and restored.status == "idle"
    assert restored.current_run is not None and restored.current_run.status == "cancelled"
    assert restored.current_run.stop_reason == "user_paused"


def test_resume_without_id_uses_latest_session_and_rejects_workspace_change(tmp_path: Path) -> None:
    service, store = shared_service(tmp_path, FinalPlanner(), ToolRegistry())
    service.run_task("pause me", mode="agent", suspend_requested=lambda: True)
    assert service.active_session is not None
    latest_id = service.active_session.session_id

    other_runner = AgentRunner(
        FinalPlanner(),
        ToolRegistry(),
        strategy="reactive",
        checkpoints=store,
        workspace_root=str((tmp_path / "moved").resolve()),
    )
    reopened = ConversationService(other_runner, store)

    assert reopened.prepare_resume().session_id == latest_id
    with pytest.raises(RuntimeError, match="belongs to workspace"):
        reopened.resume_session(interrupt=lambda _request: InterruptDecision("continue"))


def test_checkpoint_event_rolls_back_message_snapshot_and_run_status_together(tmp_path: Path) -> None:
    service, _store = shared_service(tmp_path, FinalPlanner(), ToolRegistry())
    completed = service.run_task("finish first", mode="agent")
    assert service.runtime is not None

    with psycopg.connect(os.environ["TEST_DATABASE_URL"]) as connection:
        before_messages = connection.execute(
            "SELECT COUNT(*) FROM session_runtime_messages WHERE run_id = %s",
            (completed.run_id,),
        ).fetchone()[0]
        connection.execute(
            """
            CREATE FUNCTION reject_checkpoint() RETURNS trigger AS
            'BEGIN RAISE EXCEPTION ''checkpoint rejected''; END;'
            LANGUAGE plpgsql
            """
        )
        connection.execute(
            """
            CREATE TRIGGER reject_checkpoint
            BEFORE INSERT ON checkpoints
            FOR EACH ROW EXECUTE FUNCTION reject_checkpoint()
            """
        )

    service.runtime.run.status = "cancelled"
    service.runtime.run.stop_reason = "user_paused"
    assert service.runtime.services.publish is not None
    with pytest.raises(psycopg.errors.RaiseException, match="checkpoint rejected"):
        service.runtime.services.publish(
            RuntimeEvent("cancelled", "Run paused by user", {"stop_reason": "user_paused"})
        )
    with psycopg.connect(os.environ["TEST_DATABASE_URL"]) as connection:
        after_messages = connection.execute(
            "SELECT COUNT(*) FROM session_runtime_messages WHERE run_id = %s",
            (completed.run_id,),
        ).fetchone()[0]
        run_status, run_payload = connection.execute(
            "SELECT status, state_json FROM runs WHERE run_id = %s",
            (completed.run_id,),
        ).fetchone()
        session_status = connection.execute(
            "SELECT status FROM session_runs WHERE run_id = %s",
            (completed.run_id,),
        ).fetchone()[0]
        runtime_payload = connection.execute(
            "SELECT state_json FROM session_runtime WHERE session_id = %s",
            (service.runtime.state.session_id,),
        ).fetchone()[0]

    assert after_messages == before_messages
    assert run_status == session_status == "completed"
    assert json.loads(run_payload)["current_run"]["status"] == "completed"
    assert json.loads(runtime_payload)["status"] == "idle"
