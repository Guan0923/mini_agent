from hashlib import sha256

from mini_agent.domain import (
    ArtifactMessage,
    RunHandoff,
    RunState,
    UserMessage,
    message_from_dict,
    message_to_dict,
)
from mini_agent.runtime.context import RunSummary, RuntimeState, text_messages


def artifact_message() -> ArtifactMessage:
    content = "# Implementation plan\n\n1. Inspect the project."
    return ArtifactMessage(
        artifact_id="artifact_1",
        content=content,
        relative_path="session_1/run_1/plan-r1.md",
        sha256=sha256(content.encode("utf-8")).hexdigest(),
        revision=1,
        created_by_run_id="run_1",
    )


def test_artifact_message_round_trips_all_metadata() -> None:
    message = artifact_message()

    payload = message_to_dict(message)
    restored = message_from_dict(payload)

    assert payload["role"] == "artifact"
    assert payload["source_role"] == "assistant"
    assert restored == message


def test_run_state_round_trips_artifact_links_and_handoff() -> None:
    state = RunState(
        task="Prepare a plan",
        mode="plan",
        artifact_ids=["artifact_1", "artifact_2"],
        input_artifact_ids=["artifact_input"],
        handoff=RunHandoff(
            mode="agent",
            task="Implement the plan",
            artifact_id="artifact_2",
            new_session=True,
        ),
    )

    restored = RunState.from_dict(state.to_dict())

    assert restored.artifact_ids == ["artifact_1", "artifact_2"]
    assert restored.input_artifact_ids == ["artifact_input"]
    assert restored.handoff == RunHandoff(
        mode="agent",
        task="Implement the plan",
        artifact_id="artifact_2",
        new_session=True,
    )


def test_run_handoff_defaults_new_session_for_legacy_snapshot() -> None:
    restored = RunState.from_dict(
        {
            "task": "Prepare a plan",
            "mode": "plan",
            "run_id": "run_legacy",
            "handoff": {
                "mode": "agent",
                "task": "Implement the plan",
                "artifact_id": "artifact_1",
            },
        }
    )

    assert restored.handoff == RunHandoff(
        mode="agent",
        task="Implement the plan",
        artifact_id="artifact_1",
        new_session=False,
    )


def test_new_state_fields_default_when_loading_legacy_snapshots() -> None:
    run = RunState.from_dict({"task": "Legacy", "mode": "agent", "run_id": "run_legacy"})
    runtime = RuntimeState.from_dict(
        {
            "session_id": "session_legacy",
            "run_history": [
                {
                    "run_id": "run_legacy",
                    "task": "Legacy",
                    "status": "completed",
                    "mode": "agent",
                }
            ],
        }
    )

    assert run.artifact_ids == []
    assert run.input_artifact_ids == []
    assert run.handoff is None
    assert runtime.pending_plan_artifact_id is None
    assert runtime.run_history[0].artifact_ids == []


def test_runtime_state_and_text_projection_preserve_plan_artifact() -> None:
    artifact = artifact_message()
    state = RuntimeState(
        session_id="session_1",
        messages=[UserMessage(content="Make a plan"), artifact],
        run_history=[
            RunSummary(
                run_id="run_1",
                task="Make a plan",
                status="completed",
                mode="plan",
                artifact_ids=[artifact.artifact_id],
            )
        ],
        pending_plan_artifact_id=artifact.artifact_id,
    )

    restored = RuntimeState.from_dict(state.to_dict())

    assert restored.pending_plan_artifact_id == artifact.artifact_id
    assert restored.run_history[0].artifact_ids == [artifact.artifact_id]
    assert text_messages(restored.messages) == [
        {"role": "user", "content": "Make a plan"},
        {"role": "assistant", "content": artifact.content},
    ]
