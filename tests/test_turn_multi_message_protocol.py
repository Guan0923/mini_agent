from __future__ import annotations

import pytest

from backend.domain.runtime_state import (
    InMemoryNodeStore,
    NodeFrame,
    NodeWriter,
    RuntimeState,
    RuntimeStateValidationError,
)
from backend.runtime.core.context import _chat_messages_from_nodes
from backend.runtime.core.events import RuntimeEvent
from backend.runtime.node_bridge import RuntimeEventNodeBridge


def make_running_turn() -> RuntimeState:
    return RuntimeState.create(
        session_id="session_1",
        thread_id="session_1",
        id="turn_1",
        user_content=[{"type": "text", "text": "start", "status": "success"}],
        provider_name="local",
    )


def test_running_turn_may_end_in_user_but_terminal_turn_must_end_in_assistant() -> None:
    running = make_running_turn()
    running.data[0].append(
        {
            "role": "user",
            "steering_id": "steer_1",
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
    writer = NodeWriter(InMemoryNodeStore(), emit=frames.append)
    turn = writer.create(make_running_turn())
    turn = writer.append_message(
        turn,
        {
            "role": "user",
            "steering_id": "steer_1",
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
                "steering_id": "steer_1",
                "content": "redirect",
                "references": [{"source": "project", "path": "README.md"}],
            },
        )
    )
    after_user = bridge.writer.current("session_1", "turn_1")
    assert [message["role"] for message in after_user.data[0]] == ["user", "assistant", "user"]
    assert after_user.data[0][-1]["steering_id"] == "steer_1"

    bridge.handle(RuntimeEvent("response_delta", "new answer"))
    completed = bridge.finish("success")
    assert completed is not None
    assert [message["role"] for message in completed.data[0]] == ["user", "assistant", "user", "assistant"]
    assert completed.data[0][-1]["content"] == [{"type": "text", "text": "new answer", "status": "success"}]


def test_runtime_model_history_includes_every_same_turn_message_and_file_reference() -> None:
    turn = make_running_turn()
    turn.data[0][1]["content"] = [{"type": "text", "text": "first answer", "status": "success"}]
    turn.data[0].extend(
        [
            {
                "role": "user",
                "steering_id": "steer_1",
                "content": [
                    {
                        "type": "text",
                        "text": "redirect",
                        "status": "success",
                        "references": [{"source": "project", "path": "README.md"}],
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
    assert history[2].content == "redirect\n\nFile references:\n- @README.md (project)"
