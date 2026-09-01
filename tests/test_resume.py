from pathlib import Path

import pytest

from backend.domain import (
    AssistantMessage,
    NodeFrame,
    RecoveryCheckpoint,
    RunProvenance,
    RunState,
    ToolMessage,
    TurnTrace,
    TurnTraceContext,
    TurnTraceItem,
)
from backend.domain.runtime_state import RuntimeState as RuntimeTurnState
from backend.planning.context_management import ContextCompactionResult
from backend.runtime import AgentRunner, ConversationService
from backend.runtime.conversation.recovery import reconstruct_attempt
from backend.runtime.core.context import RuntimeState
from backend.runtime.core.contracts import InterruptDecision
from backend.runtime.core.events import RuntimeEvent
from backend.runtime.node_bridge import RuntimeEventNodeBridge
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

    def compact_context(self, runtime):
        assert runtime.services.publish is not None
        runtime.services.publish(
            RuntimeEvent(
                "context_compaction_completed",
                "Conversation context compacted manually",
                {"summary": "Resumed Plan context."},
            )
        )
        return ContextCompactionResult(True, 2, 1, "Resumed Plan context.")


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
    turn = store.find_node(result.turn_id)
    assert turn is not None and turn.status == "success"
    items = [item for message in turn.data[result.data_idx] for item in message["content"]]
    tool_call = next(item for item in items if item["type"] == "tool_call")
    assert tool_call["status"] == "failed"
    assert tool_call["replay_safe"] is False
    tool_result = next(item for item in items if item["type"] == "tool_result")
    assert tool_result["status"] == "failed"
    assert tool_result["failure_code"] == "indeterminate"
    assert tool_result["retryable"] is False
    assert any(item["type"] == "text" and "Recovered without replaying" in item["text"] for item in items)
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


@pytest.mark.parametrize(
    ("choice", "expected_modes"),
    [
        ("implement", ["plan", "agent"]),
        ("implement_and_compaction", ["plan", "plan", "agent"]),
    ],
)
def test_resumed_plan_review_continues_handoff(
    tmp_path: Path,
    choice: str,
    expected_modes: list[str],
) -> None:
    service, store = shared_service(tmp_path, PlanHandoffPlanner(), ToolRegistry())
    paused = service.run_task("plan the resumed change", mode="plan", suspend_requested=lambda: True)
    assert paused.status == "cancelled" and paused.stop_reason == "user_paused"
    assert service.active_session is not None

    def interrupt(request):
        return InterruptDecision("continue" if request.kind == "resume" else choice)

    reopened = ConversationService(service.runner, store)
    result = reopened.resume_session(service.active_session.session_id, interrupt=interrupt)

    assert result is not None and result.status == "completed" and result.mode == "agent"
    turns = [node for node in store.load_nodes(service.active_session.session_id) if isinstance(node, RuntimeTurnState)]
    assert [turn.running_mode for turn in turns] == expected_modes
    assert all(turn.status == "success" for turn in turns)
    assert all(child.parent_id == parent.id for parent, child in zip(turns, turns[1:]))
    if choice == "implement_and_compaction":
        assert turns[1].assistant_items[0]["type"] == "compaction"
        assert turns[1].assistant_items[0]["summary"] == "Resumed Plan context."


def test_web_resume_reuses_external_bridge_for_plan_compaction_handoff(tmp_path: Path) -> None:
    service, store = shared_service(tmp_path, PlanHandoffPlanner(), ToolRegistry())
    paused = service.run_task("plan through Web resume", mode="plan", suspend_requested=lambda: True)
    assert paused.status == "cancelled"
    assert service.active_session is not None
    session_id = service.active_session.session_id

    reopened = ConversationService(service.runner, store, session_id=session_id)
    assert reopened.runtime is not None
    frames: list[NodeFrame] = []
    bridge = RuntimeEventNodeBridge(
        store,
        session_id=session_id,
        thread_id=session_id,
        source_node_id=paused.turn_id,
        adopt_existing=True,
        prompt="",
        running_mode="plan",
        emit=frames.append,
    )
    bridge.bind_runtime(reopened.runtime)
    current = bridge.start()
    user_item = current.data[current.current_data_idx][0]["content"][0]
    store.initialize_turn_trace(
        session_id,
        TurnTrace(
            turn_id=current.id,
            thread_id=current.thread_id,
            data_idx=current.current_data_idx,
            context=TurnTraceContext(
                system_message="Plan resume test",
                active_skills=[],
                tools=[],
                initialized_at="2026-09-01T00:00:00+00:00",
            ),
            items=[
                TurnTraceItem(
                    sequence=1,
                    message_idx=0,
                    item_idx=0,
                    role="user",
                    item=user_item,
                    completed_at="2026-09-01T00:00:00+00:00",
                )
            ],
            last_sequence=1,
            updated_at="2026-09-01T00:00:00+00:00",
        ),
    )
    reopened.attach_runtime_node_bridge(bridge, events_external=True)

    def sink(item) -> None:
        if isinstance(item, dict):
            bridge.handle_input(item)
        else:
            bridge.handle(item)

    def interrupt(request):
        assert request.kind == "plan"
        sink(
            {
                "kind": "approval",
                "message": request.message,
                "data": {
                    "decision_id": "decision_resume_plan",
                    "kind": "plan",
                    "call_id": request.data.get("call_id"),
                    "plan": request.data.get("plan"),
                },
            }
        )
        return InterruptDecision("implement_and_compaction")

    result = reopened.resume_session(
        session_id,
        on_event=sink,
        interrupt=interrupt,
        resume_confirmed=True,
    )

    assert result is not None and result.status == "completed" and result.mode == "agent"
    assert reopened.runtime_node_bridge is bridge
    final = bridge.finish("success", result.final_answer or "")
    assert final is not None and final.status == "success"
    snapshots = [frame.turn for frame in frames if frame.type == "turn.snapshot"]
    assert len(snapshots) == 3 and all(turn is not None for turn in snapshots)
    plan, compact, agent = snapshots
    assert plan is not None and compact is not None and agent is not None
    assert compact.parent_id == plan.id and agent.parent_id == compact.id
    assert [plan.running_mode, compact.running_mode, agent.running_mode] == ["plan", "plan", "agent"]

    trace = store.load_turn_trace(session_id, plan.id, plan.current_data_idx)
    assert trace is not None
    events = [item.item.get("event") for item in trace.items]
    assert events.count("decision_requested") == 1
    assert events.count("approval_granted") == 1


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
    turn = store.find_node(completed.turn_id)
    assert turn is not None and turn.status == "success"
    assert any(
        item.get("type") == "text" and item.get("text") == "Resumed."
        for message in turn.data[completed.data_idx]
        for item in message["content"]
    )
