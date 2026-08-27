from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.api.session_store import session_store
from backend.api.state import WebAppState
from backend.api.turn_steering import TurnSteeringInbox
from backend.domain.runtime_state import RuntimeState


def test_turn_steering_inbox_consumes_one_fifo_entry_per_boundary() -> None:
    inbox = TurnSteeringInbox()
    assert inbox.put("first", {"content": [{"type": "text", "text": "one"}]})
    assert inbox.put("second", {"content": [{"type": "text", "text": "two"}]})

    assert inbox.take() == [{"steering_id": "first", "content": "one", "references": []}]
    assert inbox.take() == [{"steering_id": "second", "content": "two", "references": []}]
    assert inbox.take() == []
    inbox.close()
    assert not inbox.put("third", {"content": [{"type": "text", "text": "three"}]})


def test_steer_endpoint_accepts_only_an_active_running_turn_and_normalizes_references(tmp_path: Path) -> None:
    state = WebAppState(tmp_path / "web")
    with TestClient(create_app(state)) as client:
        sidebar = client.post("/api/sidebar-threads", json={}).json()
        store = session_store(state)
        turn = RuntimeState.create(
            session_id=sidebar["session_id"],
            thread_id=sidebar["thread_id"],
            id="turn_running",
            parent=store.ensure_root_node(sidebar["session_id"]),
            user_content=[{"type": "text", "text": "start"}],
            user="",
            provider_name="local",
        )
        store.create_node(turn)
        inbox = TurnSteeringInbox()
        state.active_turn_steering = {turn.id: inbox}

        response = client.post(
            f"/api/turns/{turn.id}/steer",
            json={
                "steering_id": "steer_1",
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": " redirect ",
                            "references": [
                                {"source": "project", "path": "README.md"},
                                {"source": "project", "path": "README.md"},
                                {"source": "upload", "path": "notes.txt"},
                            ],
                        }
                    ],
                },
            },
        )

        assert response.status_code == 202
        assert inbox.take() == [
            {
                "steering_id": "steer_1",
                "content": "redirect",
                "references": [
                    {"source": "project", "path": "README.md"},
                    {"source": "upload", "path": "notes.txt"},
                ],
            }
        ]

        state.active_turn_steering.clear()
        assert (
            client.post(
                f"/api/turns/{turn.id}/steer",
                json={
                    "steering_id": "closed",
                    "message": {"role": "user", "content": [{"type": "text", "text": "late"}]},
                },
            ).status_code
            == 409
        )

        failed = turn.clone()
        failed.status = "failed"
        store.update_node(failed)
        state.active_turn_steering[turn.id] = TurnSteeringInbox()
        assert (
            client.post(
                f"/api/turns/{turn.id}/steer",
                json={
                    "steering_id": "failed",
                    "message": {"role": "user", "content": [{"type": "text", "text": "late"}]},
                },
            ).status_code
            == 409
        )
