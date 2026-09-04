from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from redis import Redis

from backend.domain import TodoStateError
from backend.storage.todo_list import TODO_TTL_SECONDS, RedisTodoListStore


@pytest.fixture
def redis_todo_store() -> tuple[RedisTodoListStore, Redis, str]:
    prefix = f"mini-agent:test:todo:{uuid4().hex}"
    client = Redis.from_url("redis://127.0.0.1:6379/0", decode_responses=True)
    try:
        client.ping()
    except Exception as exc:
        client.close()
        pytest.skip(f"real Redis unavailable: {exc}")
    store = RedisTodoListStore(client, key_prefix=prefix)
    yield store, client, prefix
    keys = list(client.scan_iter(f"{prefix}:*"))
    if keys:
        client.delete(*keys)
    client.close()


def test_real_redis_todo_transaction_is_atomic_and_idempotent(
    redis_todo_store: tuple[RedisTodoListStore, Redis, str],
) -> None:
    store, _client, _prefix = redis_todo_store
    operations = [
        {"op": "add", "content": "same", "status": "in_progress"},
        {"op": "add", "content": "same", "status": "in_progress"},
    ]

    first = store.update(
        session_id="session",
        turn_id="turn",
        call_id="call-add",
        expected_revision=0,
        operations=operations,
    )
    replay = store.update(
        session_id="session",
        turn_id="turn",
        call_id="call-add",
        expected_revision=0,
        operations=operations,
    )
    todo_id = first.snapshot.todos[0].id

    with pytest.raises(TodoStateError, match="more than once"):
        store.update(
            session_id="session",
            turn_id="turn",
            call_id="call-invalid",
            expected_revision=1,
            operations=[
                {"op": "update", "id": todo_id, "status": "completed"},
                {"op": "remove", "id": todo_id},
            ],
        )

    assert replay == first
    assert store.snapshot("session", "turn") == first.snapshot


def test_real_redis_rejects_stale_revision_and_conflicting_call_id(
    redis_todo_store: tuple[RedisTodoListStore, Redis, str],
) -> None:
    store, _client, _prefix = redis_todo_store
    operations = [{"op": "add", "content": "work", "status": "pending"}]
    store.update(
        session_id="session",
        turn_id="turn",
        call_id="call",
        expected_revision=0,
        operations=operations,
    )

    with pytest.raises(TodoStateError) as stale:
        store.update(
            session_id="session",
            turn_id="turn",
            call_id="stale",
            expected_revision=0,
            operations=operations,
        )
    with pytest.raises(TodoStateError) as conflict:
        store.update(
            session_id="session",
            turn_id="turn",
            call_id="call",
            expected_revision=1,
            operations=[{"op": "add", "content": "different", "status": "pending"}],
        )

    assert stale.value.code == "revision_conflict"
    assert stale.value.snapshot is not None and stale.value.snapshot.revision == 1
    assert conflict.value.code == "call_id_conflict"
    assert store.snapshot("session", "turn").revision == 1


def test_real_redis_finalization_and_ttl_lifecycle(
    redis_todo_store: tuple[RedisTodoListStore, Redis, str],
) -> None:
    store, client, _prefix = redis_todo_store
    store.update(
        session_id="session",
        turn_id="turn",
        call_id="call",
        expected_revision=0,
        operations=[{"op": "add", "content": "work", "status": "pending"}],
    )
    key = store._key("session", "turn")

    assert client.ttl(key) == -1
    assert store.claim_finalization("session", "turn") is True
    assert store.claim_finalization("session", "turn") is False
    assert store.finalization_claimed("session", "turn") is True
    store.expire_turn("session", "turn")
    assert 0 < client.ttl(key) <= TODO_TTL_SECONDS
    store.persist_turn("session", "turn")
    assert client.ttl(key) == -1


def test_real_redis_allows_only_one_concurrent_writer(
    redis_todo_store: tuple[RedisTodoListStore, Redis, str],
) -> None:
    store, _client, _prefix = redis_todo_store

    def update(call_id: str):
        try:
            return store.update(
                session_id="session",
                turn_id="turn",
                call_id=call_id,
                expected_revision=0,
                operations=[{"op": "add", "content": call_id, "status": "pending"}],
            )
        except TodoStateError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(update, ["concurrent-a", "concurrent-b"]))

    assert sum(not isinstance(outcome, TodoStateError) for outcome in outcomes) == 1
    conflicts = [outcome for outcome in outcomes if isinstance(outcome, TodoStateError)]
    assert len(conflicts) == 1 and conflicts[0].code == "revision_conflict"
    assert store.snapshot("session", "turn").revision == 1


def test_real_redis_isolates_sessions_and_turns_even_when_call_ids_match(
    redis_todo_store: tuple[RedisTodoListStore, Redis, str],
) -> None:
    store, _client, _prefix = redis_todo_store
    operation = [{"op": "add", "content": "isolated", "status": "pending"}]

    first = store.update(
        session_id="session-a",
        turn_id="turn-a",
        call_id="same-call",
        expected_revision=0,
        operations=operation,
    )
    second = store.update(
        session_id="session-a",
        turn_id="turn-b",
        call_id="same-call",
        expected_revision=0,
        operations=operation,
    )
    third = store.update(
        session_id="session-b",
        turn_id="turn-a",
        call_id="same-call",
        expected_revision=0,
        operations=operation,
    )

    assert first.snapshot.revision == second.snapshot.revision == third.snapshot.revision == 1
    assert len({first.snapshot.todos[0].id, second.snapshot.todos[0].id, third.snapshot.todos[0].id}) == 3
