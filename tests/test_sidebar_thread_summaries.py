from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.api.state import WebAppState
from backend.domain.runtime_state import NodeWriter, RuntimeRootState, RuntimeState
from backend.storage import MemoryMessageQueue, SQLiteSessionStore


def _store(state: WebAppState) -> SQLiteSessionStore:
    return SQLiteSessionStore(state.paths, state.agent_thread_index)


def _turn(
    store: SQLiteSessionStore,
    *,
    session_id: str,
    thread_id: str,
    turn_id: str,
    parent: RuntimeRootState | RuntimeState,
    prompt: str,
    answer: str | None = "answer",
) -> RuntimeState:
    writer = NodeWriter(store)
    node = writer.create(
        session_id=session_id,
        thread_id=thread_id,
        id=turn_id,
        parent=parent,
        user_content=prompt,
    )
    if answer is not None:
        node = writer.append_item(node, {"type": "text", "text": answer, "status": "success"})
        node = writer.finalize(node, "success")
    return node


def _summary(client: TestClient, thread_id: str) -> dict[str, object]:
    return next(
        item
        for item in client.get("/api/sidebar-threads", params={"state": "all"}).json()
        if item["thread_id"] == thread_id
    )


def test_sidebar_summary_tracks_durable_messages_without_opening_the_conversation(tmp_path: Path) -> None:
    state = WebAppState(tmp_path / ".mini_agent", message_queue=MemoryMessageQueue())
    with TestClient(create_app(state)) as client:
        created = client.post("/api/sidebar-threads", json={}).json()
        assert created["message_count"] == 0
        assert created["conversation_updated_at"] == created["created_at"]

        store = _store(state)
        root = store.ensure_root_node(created["session_id"], id="summary-root")
        writer = NodeWriter(store)
        running = writer.create(
            session_id=created["session_id"],
            thread_id=created["thread_id"],
            id="summary-running",
            parent=root,
            user_content="question",
        )

        persisted_user = _summary(client, created["thread_id"])
        assert persisted_user["message_count"] == 1
        assert persisted_user["conversation_updated_at"] != created["conversation_updated_at"]

        running = writer.append_item(
            running,
            {"type": "text", "text": "streamed answer", "status": "success"},
        )
        writer.finalize(running, "success")
        completed = _summary(client, created["thread_id"])
        assert completed["message_count"] == 2

        renamed = client.patch(
            f"/api/sidebar-threads/{created['thread_id']}",
            json={"title": "renamed"},
        ).json()
        assert renamed["conversation_updated_at"] == completed["conversation_updated_at"]
        assert renamed["updated_at"] != created["updated_at"]


def test_sidebar_summary_follows_each_thread_head_and_excludes_sibling_branches(tmp_path: Path) -> None:
    state = WebAppState(tmp_path / ".mini_agent", message_queue=MemoryMessageQueue())
    with TestClient(create_app(state)) as client:
        sidebar = client.post("/api/sidebar-threads", json={"title": "source"}).json()
        store = _store(state)
        root = store.ensure_root_node(sidebar["session_id"], id="branch-root")
        first = _turn(
            store,
            session_id=sidebar["session_id"],
            thread_id=sidebar["thread_id"],
            turn_id="main-first",
            parent=root,
            prompt="first",
        )
        second = _turn(
            store,
            session_id=sidebar["session_id"],
            thread_id=sidebar["thread_id"],
            turn_id="main-second",
            parent=first,
            prompt="second",
        )

        fork_response = client.post(
            f"/api/turns/{first.id}/fork",
            json={"id": "fork-copy", "thread_id": "thread-fork"},
        )
        assert fork_response.status_code == 201
        fork_payload = fork_response.json()
        assert fork_payload["sidebar_thread"]["message_count"] == 2
        forked = store.get_node(sidebar["session_id"], "fork-copy")
        assert isinstance(forked, RuntimeState)

        _turn(
            store,
            session_id=sidebar["session_id"],
            thread_id=sidebar["thread_id"],
            turn_id="main-third",
            parent=second,
            prompt="main only",
        )
        _turn(
            store,
            session_id=sidebar["session_id"],
            thread_id="thread-fork",
            turn_id="fork-second",
            parent=forked,
            prompt="fork only",
        )

        summaries = client.get("/api/sidebar-threads", params={"state": "all"}).json()
        by_thread = {item["thread_id"]: item for item in summaries}
        assert by_thread[sidebar["thread_id"]]["message_count"] == 6
        assert by_thread["thread-fork"]["message_count"] == 4
        assert [item["conversation_updated_at"] for item in summaries] == sorted(
            (item["conversation_updated_at"] for item in summaries),
            reverse=True,
        )

        rewound = store.append_turn_version(
            second.id,
            {"type": "text", "text": "second rewritten", "status": "success"},
        )
        rewound.data[rewound.current_data_idx][-1]["content"] = [
            {"type": "text", "text": "rewritten answer", "status": "success"}
        ]
        rewound.status = "success"
        store.finalize_node(rewound)

        assert _summary(client, sidebar["thread_id"])["message_count"] == 4
        assert _summary(client, "thread-fork")["message_count"] == 4


def test_sidebar_summary_crud_responses_keep_the_same_contract(tmp_path: Path) -> None:
    state = WebAppState(tmp_path / ".mini_agent", message_queue=MemoryMessageQueue())
    with TestClient(create_app(state)) as client:
        created = client.post("/api/sidebar-threads", json={}).json()
        expected = {"message_count", "conversation_updated_at"}
        assert expected <= created.keys()
        assert [item["thread_id"] for item in client.get("/api/sidebar-threads?state=active").json()] == [
            created["thread_id"]
        ]

        archived = client.post(f"/api/sidebar-threads/{created['thread_id']}/archive").json()
        assert [item["thread_id"] for item in client.get("/api/sidebar-threads?state=archived").json()] == [
            created["thread_id"]
        ]
        restored = client.post(f"/api/sidebar-threads/{created['thread_id']}/restore").json()
        assert [item["thread_id"] for item in client.get("/api/sidebar-threads?state=active").json()] == [
            created["thread_id"]
        ]
        deleted = client.delete(f"/api/sidebar-threads/{created['thread_id']}").json()
        assert expected <= archived.keys()
        assert expected <= restored.keys()
        assert expected <= deleted.keys()
        assert [item["thread_id"] for item in client.get("/api/sidebar-threads?state=deleted").json()] == [
            created["thread_id"]
        ]
        assert [item["thread_id"] for item in client.get("/api/sidebar-threads?state=all").json()] == [
            created["thread_id"]
        ]
