from __future__ import annotations

from pathlib import Path

import pytest

from backend.domain.runtime_state import (
    InMemoryNodeStore,
    NodeFrame,
    NodeWriter,
    RuntimeRootState,
    RuntimeState,
    RuntimeStateValidationError,
)
from backend.runtime.conversation.steering import _model_content_with_references
from backend.runtime.core.context import _chat_messages_from_nodes
from backend.runtime.core.events import RuntimeEvent
from backend.runtime.node_bridge import RuntimeEventNodeBridge


def make_running_turn(*, parent: RuntimeRootState | None = None) -> RuntimeState:
    return RuntimeState.create(
        session_id="session_1",
        thread_id="session_1",
        id="turn_1",
        parent=parent,
        user_content=[{"type": "text", "text": "start", "status": "success"}],
        provider_name="local",
    )


def test_running_turn_may_end_in_user_but_terminal_turn_must_end_in_assistant() -> None:
    running = make_running_turn()
    running.data[0].append(
        {
            "role": "user",
            "delivery_id": "delivery_1",
            "content": [{"type": "text", "text": "redirect", "status": "success"}],
        }
    )
    running = RuntimeState.from_dict(running.to_dict())
    assert [message["role"] for message in running.data[0]] == ["user", "assistant", "user"]

    failed = running.to_dict()
    failed["status"] = "failed"
    with pytest.raises(RuntimeStateValidationError, match="must end with an assistant"):
        RuntimeState.from_dict(failed)


def test_writer_emits_append_message_then_message_indexed_item_and_text_operations() -> None:
    frames: list[NodeFrame] = []
    store = InMemoryNodeStore()
    writer = NodeWriter(store, emit=frames.append)
    turn = writer.create(make_running_turn(parent=store.ensure_root_node("session_1", id="turn_root")))
    turn = writer.append_message(
        turn,
        {
            "role": "user",
            "delivery_id": "delivery_1",
            "content": [{"type": "text", "text": "redirect", "status": "success"}],
        },
    )
    turn = writer.append_message(turn, {"role": "assistant", "content": []})
    turn = writer.append_items(
        turn,
        [{"type": "text", "text": "new ", "status": "running"}],
        message_idx=3,
        persist=False,
    )
    turn = writer.append_text(turn, data_idx=0, message_idx=3, item_idx=0, delta="answer", persist=True)

    assert frames[1].operations[0]["op"] == "append_message"
    assert frames[1].operations[0]["message_idx"] == 2
    assert frames[3].operations == (
        {
            "op": "append_item",
            "data_idx": 0,
            "message_idx": 3,
            "item_idx": 0,
            "item": {"type": "text", "text": "new ", "status": "running"},
        },
    )
    assert frames[4].operations[0]["message_idx"] == 3


def test_runtime_bridge_appends_canonical_user_before_starting_the_next_assistant() -> None:
    store = InMemoryNodeStore()
    frames: list[NodeFrame] = []
    bridge = RuntimeEventNodeBridge(
        store,
        session_id="session_1",
        thread_id="session_1",
        turn_id="turn_1",
        prompt="start",
        provider_name="local",
        emit=frames.append,
    )
    bridge.start()
    bridge.handle(
        RuntimeEvent(
            "steering_applied",
            data={
                "delivery_id": "delivery_1",
                "content": "redirect",
                "references": [
                    {
                        "source": "project",
                        "path": "C:/workspace/README.md",
                        "display_path": "README.md",
                    }
                ],
            },
        )
    )
    after_user = bridge.writer.current("session_1", "turn_1")
    assert [message["role"] for message in after_user.data[0]] == ["user", "assistant", "user"]
    assert after_user.data[0][-1]["delivery_id"] == "delivery_1"

    bridge.handle(
        RuntimeEvent(
            "steering_applied",
            data={"delivery_id": "delivery_1", "content": "redirect"},
        )
    )
    deduplicated = bridge.writer.current("session_1", "turn_1")
    assert [message["role"] for message in deduplicated.data[0]] == ["user", "assistant", "user"]

    bridge.handle(RuntimeEvent("response_delta", "new answer"))
    completed = bridge.finish("success")
    assert completed is not None
    assert [message["role"] for message in completed.data[0]] == ["user", "assistant", "user", "assistant"]
    assert completed.data[0][-1]["content"] == [{"type": "text", "text": "new answer", "status": "success"}]


def test_runtime_model_history_maps_structured_references_to_absolute_workspace_paths(tmp_path: Path) -> None:
    turn = make_running_turn()
    session_workspace = tmp_path / "session"
    project_workspace = tmp_path / "project"
    (session_workspace / "uploads").mkdir(parents=True)
    project_workspace.mkdir()
    turn.cwd = str(session_workspace.resolve())
    turn.project_cwd = str(project_workspace.resolve())
    turn.data[0][1]["content"] = [{"type": "text", "text": "first answer", "status": "success"}]
    turn.data[0].extend(
        [
            {
                "role": "user",
                "delivery_id": "delivery_1",
                "content": [
                    {
                        "type": "text",
                        "text": "redirect",
                        "status": "success",
                        "references": [
                            {
                                "source": "project",
                                "path": str((project_workspace / "README.md").resolve()),
                                "display_path": "README.md",
                            },
                            {
                                "source": "upload",
                                "path": str((session_workspace / "uploads" / "notes.txt").resolve()),
                                "display_path": "notes.txt",
                            },
                        ],
                    }
                ],
            },
            {
                "role": "assistant",
                "content": [{"type": "text", "text": "second answer", "status": "success"}],
            },
        ]
    )
    turn = RuntimeState.from_dict(turn.to_dict())

    history = _chat_messages_from_nodes([turn])
    assert [message.role for message in history] == ["user", "assistant", "user", "assistant"]
    assert history[2].content == (
        "redirect\n\nFile references:\n"
        f"- @{(project_workspace / 'README.md').resolve().as_posix()} (project)\n"
        f"- @{(session_workspace / 'uploads' / 'notes.txt').resolve().as_posix()} (upload)"
    )


def test_plain_text_at_path_is_not_expanded_before_model_projection() -> None:
    turn = make_running_turn()
    turn.data[0][0]["content"][0]["text"] = "Please inspect @secret.txt"

    history = _chat_messages_from_nodes([RuntimeState.from_dict(turn.to_dict())])

    assert history[0].content == "Please inspect @secret.txt"


def test_running_steering_model_content_uses_validated_absolute_references(tmp_path: Path) -> None:
    project_file = (tmp_path / "README.md").resolve()
    upload_file = (tmp_path / "uploads" / "notes.txt").resolve()

    content = _model_content_with_references(
        "redirect",
        (
            {"source": "project", "path": str(project_file), "display_path": "README.md"},
            {"source": "upload", "path": str(upload_file), "display_path": "notes.txt"},
        ),
    )

    assert content == (
        f"redirect\n\nFile references:\n- @{project_file.as_posix()} (project)\n- @{upload_file.as_posix()} (upload)"
    )
