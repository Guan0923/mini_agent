from pathlib import Path

import pytest

from backend.domain import (
    AssistantMessage,
    RecoveryCheckpoint,
    RunProvenance,
    RunState,
    ToolMessage,
)
from backend.runtime import AgentRunner, ConversationService
from backend.runtime.conversation.recovery import reconstruct_attempt
from backend.runtime.core.context import RuntimeState
from backend.runtime.core.contracts import InterruptDecision
from backend.runtime.planning.review import REQUEST_PLAN_REVIEW_NAME
from backend.tools import Tool, ToolRegistry
from tests.local_store import session_store


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


def shared_service(tmp_path: Path, planner, tools: ToolRegistry):
    store = session_store(tmp_path / "store")
    runner = AgentRunner(
        planner,
        tools,
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
    import json
    import sqlite3

    with sqlite3.connect(store.paths.session_db(service.active_session.session_id)) as connection:
        rows = connection.execute(
            "SELECT namespace,payload_json FROM json_objects WHERE namespace IN ('checkpoint','run')"
        ).fetchall()
    objects = [(namespace, json.loads(payload)) for namespace, payload in rows]
    checkpoint_reasons = {
        payload["reason"]
        for namespace, payload in objects
        if namespace == "checkpoint" and payload.get("run_id") == paused.run_id
    }
    workflow_attempts = sorted(
        int(payload["provenance"]["attempt"])
        for namespace, payload in objects
        if namespace == "run" and payload.get("provenance", {}).get("workflow_id") == workflow_id
    )
    assert "run_cancelled" in checkpoint_reasons
    assert workflow_attempts == [1, 2]


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
        checkpoints=store,
        workspace_root=str((tmp_path / "moved").resolve()),
    )
    reopened = ConversationService(other_runner, store)

    assert reopened.prepare_resume().session_id == latest_id
    with pytest.raises(RuntimeError, match="belongs to workspace"):
        reopened.resume_session(interrupt=lambda _request: InterruptDecision("continue"))


def test_checkpoint_event_persists_local_sqlite_state(tmp_path: Path) -> None:
    service, store = shared_service(tmp_path, FinalPlanner(), ToolRegistry())
    completed = service.run_task("finish first", mode="agent")
    assert service.runtime is not None

    restored = store.load_runtime(service.runtime.state.session_id)
    assert restored is not None
    assert restored.current_run is not None
    assert restored.current_run.run_id == completed.run_id
    assert restored.current_run.status == "completed"
    assert store.load_runtime_messages(service.runtime.state.session_id, completed.run_id)
