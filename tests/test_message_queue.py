from __future__ import annotations

import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from redis import Redis

from backend.api.app import create_app
from backend.api.session_store import session_store
from backend.api.state import WebAppState
from backend.domain import DeliveryConflict, QueuedMessage
from backend.domain.runtime_state import RuntimeState
from backend.storage.message_queue import STALE_CLAIM_MS, RedisMessageQueue


def test_queued_message_api_is_ordered_idempotent_and_restricts_dispatched_mutations(tmp_path: Path) -> None:
    state = WebAppState(tmp_path / "web")
    with TestClient(create_app(state)) as client:
        sidebar = client.post("/api/sidebar-threads", json={}).json()
        thread_id = sidebar["thread_id"]
        first_id = str(uuid4())
        second_id = str(uuid4())
        first = {
            "id": first_id,
            "content": "first",
            "references": [{"source": "project", "path": "README.md"}],
        }
        assert client.post(f"/api/sidebar-threads/{thread_id}/queued-messages", json=first).status_code == 201
        assert client.post(f"/api/sidebar-threads/{thread_id}/queued-messages", json=first).status_code == 200
        assert (
            client.post(
                f"/api/sidebar-threads/{thread_id}/queued-messages",
                json={**first, "content": "conflict"},
            ).status_code
            == 409
        )
        assert (
            client.post(
                f"/api/sidebar-threads/{thread_id}/queued-messages",
                json={"id": second_id, "content": "second", "references": []},
            ).status_code
            == 201
        )
        updated = client.patch(
            f"/api/sidebar-threads/{thread_id}/queued-messages/{second_id}",
            json={"content": "second edited", "references": []},
        )
        assert updated.status_code == 200
        assert [item["content"] for item in client.get(f"/api/sidebar-threads/{thread_id}/queued-messages").json()] == [
            "first",
            "second edited",
        ]

        state.message_queue.dispatch(
            delivery_id="delivery-api",
            message_ids=[first_id],
            session_id=sidebar["session_id"],
            thread_id=thread_id,
            turn_id="turn-api",
        )
        assert (
            client.patch(
                f"/api/sidebar-threads/{thread_id}/queued-messages/{first_id}",
                json={"content": "late", "references": []},
            ).status_code
            == 409
        )
        assert client.delete(f"/api/sidebar-threads/{thread_id}/queued-messages/{first_id}").status_code == 409
        assert client.delete(f"/api/sidebar-threads/{thread_id}/queued-messages/{second_id}").status_code == 204


def test_create_turn_accepts_exactly_one_message_source_and_claims_queued_delivery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from backend.api.routes import turns as turn_routes

    captured: dict[str, object] = {}

    def fake_stream_turn(_state, **kwargs):
        captured.update(kwargs)
        return JSONResponse({"accepted": True})

    monkeypatch.setattr(turn_routes, "_stream_turn", fake_stream_turn)
    state = WebAppState(tmp_path / "web")
    with TestClient(create_app(state)) as client:
        sidebar = client.post("/api/sidebar-threads", json={}).json()
        message_id = str(uuid4())
        assert (
            client.post(
                f"/api/sidebar-threads/{sidebar['thread_id']}/queued-messages",
                json={"id": message_id, "content": "queued turn", "references": []},
            ).status_code
            == 201
        )
        base = {
            "id": "turn-queued",
            "session_id": sidebar["session_id"],
            "thread_id": sidebar["thread_id"],
        }
        assert client.post("/api/turns", json=base).status_code == 422
        assert (
            client.post(
                "/api/turns",
                json={
                    **base,
                    "message": {"role": "user", "content": [{"type": "text", "text": "duplicate"}]},
                    "queued_delivery": {"delivery_id": "delivery-create", "message_ids": [message_id]},
                },
            ).status_code
            == 422
        )
        response = client.post(
            "/api/turns",
            json={
                **base,
                "queued_delivery": {"delivery_id": "delivery-create", "message_ids": [message_id]},
            },
        )
        assert response.status_code == 200
        assert captured["prompt"] == "queued turn"
        assert captured["references"] == []
        initial = captured["initial_delivery"]
    assert initial.envelope.delivery_id == "delivery-create"


def test_create_turn_returns_claimed_delivery_to_pending_when_stream_setup_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from backend.api.routes import turns as turn_routes
    from backend.storage.message_queue import MemoryMessageQueue

    def fail_stream_turn(_state, **_kwargs):
        raise RuntimeError("stream setup failed")

    monkeypatch.setattr(turn_routes, "_stream_turn", fail_stream_turn)
    queue = MemoryMessageQueue()
    state = WebAppState(tmp_path / "web", message_queue=queue)
    with TestClient(create_app(state), raise_server_exceptions=False) as client:
        sidebar = client.post("/api/sidebar-threads", json={}).json()
        message_id = str(uuid4())
        queue.create(QueuedMessage(message_id, sidebar["thread_id"], "retry later"))
        response = client.post(
            "/api/turns",
            json={
                "id": "turn-stream-failure",
                "session_id": sidebar["session_id"],
                "thread_id": sidebar["thread_id"],
                "queued_delivery": {
                    "delivery_id": "delivery-stream-failure",
                    "message_ids": [message_id],
                },
            },
        )

    assert response.status_code == 500
    assert [(item.id, item.state) for item in queue.list(sidebar["thread_id"])] == [(message_id, "pending")]


@pytest.fixture
def redis_queue() -> RedisMessageQueue:
    prefix = f"mini-agent:test:{uuid4().hex}"
    client = Redis.from_url("redis://127.0.0.1:6379/0", decode_responses=True)
    try:
        client.ping()
    except Exception as exc:
        pytest.skip(f"real Redis unavailable: {exc}")
    queue = RedisMessageQueue(client, key_prefix=prefix)
    yield queue
    keys = list(client.scan_iter(f"{prefix}:*"))
    if keys:
        client.delete(*keys)
    client.close()


def test_real_redis_dispatch_claim_ack_and_receipt_replay(redis_queue: RedisMessageQueue) -> None:
    thread_id = "thread-real"
    redis_queue.create(QueuedMessage("one", thread_id, "one", ({"source": "project", "path": "a"},)))
    redis_queue.create(QueuedMessage("two", thread_id, "two", ({"source": "project", "path": "a"},)))
    redis_queue.create(QueuedMessage("three", thread_id, "three"))

    first = redis_queue.dispatch(
        delivery_id="delivery-one",
        message_ids=["two", "one"],
        session_id="session-real",
        thread_id=thread_id,
        turn_id="turn-real",
    )
    assert redis_queue.client.ttl(redis_queue._receipt_key("delivery-one")) == -1
    redis_queue.dispatch(
        delivery_id="delivery-two",
        message_ids=["three"],
        session_id="session-real",
        thread_id=thread_id,
        turn_id="turn-real",
    )
    assert first.source_message_ids == ("one", "two")
    assert first.content == "one\n\ntwo"
    assert first.references == ({"source": "project", "path": "a"},)

    claimed_first = redis_queue.claim("turn-real", "consumer-a")
    assert claimed_first is not None and claimed_first.envelope.delivery_id == "delivery-one"
    redis_queue.ack(claimed_first)
    assert 0 < redis_queue.client.ttl(redis_queue._receipt_key("delivery-one")) <= 7 * 24 * 60 * 60
    claimed_second = redis_queue.claim("turn-real", "consumer-a")
    assert claimed_second is not None and claimed_second.envelope.delivery_id == "delivery-two"
    redis_queue.ack(claimed_second)
    assert redis_queue.list(thread_id) == []
    assert redis_queue.client.exists(redis_queue._stream_key("turn-real")) == 0
    redis_queue.ack(claimed_second)
    assert redis_queue.client.exists(redis_queue._stream_key("turn-real")) == 0

    assert (
        redis_queue.dispatch(
            delivery_id="delivery-one",
            message_ids=["one", "two"],
            session_id="session-real",
            thread_id=thread_id,
            turn_id="turn-real",
        )
        == first
    )
    with pytest.raises(DeliveryConflict):
        redis_queue.dispatch(
            delivery_id="delivery-one",
            message_ids=["three"],
            session_id="session-real",
            thread_id=thread_id,
            turn_id="turn-real",
        )


def test_real_redis_reclaims_stale_delivery_and_returns_unconsumed_messages(redis_queue: RedisMessageQueue) -> None:
    redis_queue.create(QueuedMessage("one", "thread", "one"))
    redis_queue.create(QueuedMessage("two", "thread", "two"))
    redis_queue.dispatch(
        delivery_id="delivery-stale",
        message_ids=["one"],
        session_id="session",
        thread_id="thread",
        turn_id="turn",
    )
    redis_queue.dispatch(
        delivery_id="delivery-pending",
        message_ids=["two"],
        session_id="session",
        thread_id="thread",
        turn_id="turn",
    )
    stale = redis_queue.claim("turn", "crashed-worker")
    assert stale is not None
    stream = redis_queue._stream_key("turn")
    assert redis_queue.claim("turn", "replacement-worker") is None
    redis_queue.client.xclaim(
        stream,
        redis_queue.consumer_group,
        "crashed-worker",
        min_idle_time=0,
        message_ids=[stale.stream_id],
        idle=STALE_CLAIM_MS + 1,
    )
    reclaimed = redis_queue.claim("turn", "replacement-worker")
    assert reclaimed is not None and reclaimed.envelope.delivery_id == "delivery-stale"
    redis_queue.ack(reclaimed)

    redis_queue.release_turn("turn")
    remaining = redis_queue.list("thread")
    assert [(item.id, item.state) for item in remaining] == [("two", "pending")]
    assert redis_queue.client.exists(stream) == 0
    replay = redis_queue.dispatch(
        delivery_id="delivery-pending",
        message_ids=["two"],
        session_id="session",
        thread_id="thread",
        turn_id="turn",
    )
    assert replay.delivery_id == "delivery-pending"
    replayed_claim = redis_queue.claim("turn", "replacement-worker")
    assert replayed_claim is not None and replayed_claim.envelope.delivery_id == "delivery-pending"


def test_startup_reconciliation_acks_double_persisted_delivery_and_fails_running_turn(tmp_path: Path) -> None:
    from backend.storage.message_queue import MemoryMessageQueue

    queue = MemoryMessageQueue()
    state = WebAppState(tmp_path / "web", message_queue=queue)
    with TestClient(create_app(state)) as client:
        sidebar = client.post("/api/sidebar-threads", json={}).json()
    store = session_store(state)
    queue.create(QueuedMessage("message", sidebar["thread_id"], "persist me"))
    queue.dispatch(
        delivery_id="delivery-persisted",
        message_ids=["message"],
        session_id=sidebar["session_id"],
        thread_id=sidebar["thread_id"],
        turn_id="turn-persisted",
    )
    root = store.ensure_root_node(sidebar["session_id"])
    node = RuntimeState.create(
        session_id=sidebar["session_id"],
        thread_id=sidebar["thread_id"],
        id="turn-persisted",
        parent=root,
        user_content=[{"type": "text", "text": "persist me", "status": "success"}],
    )
    node.data[0][0]["delivery_id"] = "delivery-persisted"
    store.create_node(RuntimeState.from_dict(node.to_dict()))
    store.start_turn(sidebar["session_id"], "run-persisted", "persist me", delivery_id="delivery-persisted")

    reopened = WebAppState(tmp_path / "web", message_queue=queue)
    reopened_store = session_store(reopened)
    reconciled = reopened_store.find_node("turn-persisted")
    assert isinstance(reconciled, RuntimeState) and reconciled.status == "failed"
    assert reopened_store.get_session_summary(sidebar["session_id"]).last_run_status == "failed"
    assert queue.list(sidebar["thread_id"]) == []
    reopened.close()


def test_sqlite_turn_message_delivery_is_idempotent(tmp_path: Path) -> None:
    state = WebAppState(tmp_path / "web")
    with TestClient(create_app(state)) as client:
        sidebar = client.post("/api/sidebar-threads", json={}).json()
    store = session_store(state)
    store.start_turn(sidebar["session_id"], "run", "start")
    store.append_turn_input(sidebar["session_id"], "run", "redirect", delivery_id="delivery")
    store.append_turn_input(sidebar["session_id"], "run", "redirect", delivery_id="delivery")

    with sqlite3.connect(state.paths.session_db(sidebar["session_id"])) as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM json_objects WHERE namespace='turn_message' "
            "AND json_extract(payload_json, '$.delivery_id')='delivery'"
        ).fetchone()[0]
    assert count == 1


def test_startup_reconciliation_returns_delivery_that_never_reached_sqlite(tmp_path: Path) -> None:
    from backend.storage.message_queue import MemoryMessageQueue

    queue = MemoryMessageQueue()
    state = WebAppState(tmp_path / "web", message_queue=queue)
    with TestClient(create_app(state)) as client:
        sidebar = client.post("/api/sidebar-threads", json={}).json()
    queue.create(QueuedMessage("message", sidebar["thread_id"], "retry me"))
    queue.dispatch(
        delivery_id="delivery-unpersisted",
        message_ids=["message"],
        session_id=sidebar["session_id"],
        thread_id=sidebar["thread_id"],
        turn_id="turn-missing",
    )

    reopened = WebAppState(tmp_path / "web", message_queue=queue)
    assert [(item.id, item.state) for item in queue.list(sidebar["thread_id"])] == [("message", "pending")]
    reopened.close()


def test_startup_reconciliation_repairs_missing_canonical_projection(tmp_path: Path) -> None:
    from backend.storage.message_queue import MemoryMessageQueue

    queue = MemoryMessageQueue()
    state = WebAppState(tmp_path / "web", message_queue=queue)
    with TestClient(create_app(state)) as client:
        sidebar = client.post("/api/sidebar-threads", json={}).json()
    store = session_store(state)
    queue.create(QueuedMessage("message", sidebar["thread_id"], "repair canonical"))
    queue.dispatch(
        delivery_id="delivery-canonical",
        message_ids=["message"],
        session_id=sidebar["session_id"],
        thread_id=sidebar["thread_id"],
        turn_id="turn-canonical",
    )
    root = store.ensure_root_node(sidebar["session_id"])
    node = RuntimeState.create(
        session_id=sidebar["session_id"],
        thread_id=sidebar["thread_id"],
        id="turn-canonical",
        parent=root,
        user_content=[{"type": "text", "text": "start", "status": "success"}],
    )
    store.create_node(node)
    store.start_turn(
        sidebar["session_id"],
        "run-canonical",
        "repair canonical",
        delivery_id="delivery-canonical",
    )

    reopened = WebAppState(tmp_path / "web", message_queue=queue)
    repaired = session_store(reopened).find_node("turn-canonical")
    assert isinstance(repaired, RuntimeState) and repaired.status == "failed"
    assert any(
        message.get("delivery_id") == "delivery-canonical" for message in repaired.data[repaired.current_data_idx]
    )
    assert queue.list(sidebar["thread_id"]) == []
    reopened.close()


def test_startup_reconciliation_repairs_missing_turn_message_projection(tmp_path: Path) -> None:
    from backend.storage.message_queue import MemoryMessageQueue

    queue = MemoryMessageQueue()
    state = WebAppState(tmp_path / "web", message_queue=queue)
    with TestClient(create_app(state)) as client:
        sidebar = client.post("/api/sidebar-threads", json={}).json()
    store = session_store(state)
    queue.create(QueuedMessage("message", sidebar["thread_id"], "repair turn message"))
    queue.dispatch(
        delivery_id="delivery-turn-message",
        message_ids=["message"],
        session_id=sidebar["session_id"],
        thread_id=sidebar["thread_id"],
        turn_id="turn-message",
    )
    root = store.ensure_root_node(sidebar["session_id"])
    node = RuntimeState.create(
        session_id=sidebar["session_id"],
        thread_id=sidebar["thread_id"],
        id="turn-message",
        parent=root,
        user_content=[{"type": "text", "text": "repair turn message", "status": "success"}],
    )
    node.data[0][0]["delivery_id"] = "delivery-turn-message"
    store.create_node(RuntimeState.from_dict(node.to_dict()))
    store.start_turn(sidebar["session_id"], "run-turn-message", "start")

    reopened = WebAppState(tmp_path / "web", message_queue=queue)
    repaired_store = session_store(reopened)
    assert repaired_store.has_turn_delivery(sidebar["session_id"], "delivery-turn-message")
    assert queue.list(sidebar["thread_id"]) == []
    reopened.close()


def test_startup_reconciliation_does_not_block_history_when_redis_drops_during_ack(tmp_path: Path) -> None:
    from backend.domain import MessageQueueUnavailable
    from backend.storage.message_queue import MemoryMessageQueue

    class AckUnavailableQueue(MemoryMessageQueue):
        def ack(self, claimed) -> None:
            del claimed
            raise MessageQueueUnavailable("message_queue_unavailable")

    queue = AckUnavailableQueue()
    state = WebAppState(tmp_path / "web", message_queue=queue)
    with TestClient(create_app(state)) as client:
        sidebar = client.post("/api/sidebar-threads", json={}).json()
    store = session_store(state)
    queue.create(QueuedMessage("message", sidebar["thread_id"], "persist me"))
    queue.dispatch(
        delivery_id="delivery-ack-down",
        message_ids=["message"],
        session_id=sidebar["session_id"],
        thread_id=sidebar["thread_id"],
        turn_id="turn-ack-down",
    )
    root = store.ensure_root_node(sidebar["session_id"])
    node = RuntimeState.create(
        session_id=sidebar["session_id"],
        thread_id=sidebar["thread_id"],
        id="turn-ack-down",
        parent=root,
        user_content=[{"type": "text", "text": "persist me", "status": "success"}],
    )
    node.data[0][0]["delivery_id"] = "delivery-ack-down"
    store.create_node(RuntimeState.from_dict(node.to_dict()))
    store.start_turn(sidebar["session_id"], "run-ack-down", "persist me", delivery_id="delivery-ack-down")

    reopened = WebAppState(tmp_path / "web", message_queue=queue)
    assert isinstance(session_store(reopened).find_node("turn-ack-down"), RuntimeState)
    assert queue.list(sidebar["thread_id"])[0].state == "dispatched"
    reopened.close()
