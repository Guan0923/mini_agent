from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from redis import Redis

from backend.api.runtime_event_transport import (
    publish_frame,
    publish_outbox_event,
    publish_terminal,
    thread_sse,
    turn_sse,
)
from backend.configuration import ClientPaths
from backend.domain.runtime_state import NodeFrame, RuntimeState, utc_iso
from backend.storage import runtime_event_stream as event_stream_module
from backend.storage.runtime_event_stream import EVENT_STREAM_TTL_SECONDS, RedisRuntimeEventStream
from backend.storage.sqlite import SQLiteSessionStore


@pytest.fixture
def redis_event_stream() -> tuple[RedisRuntimeEventStream, Redis, str]:
    prefix = f"mini-agent:test:runtime-events:{uuid4().hex}"
    client = Redis.from_url("redis://127.0.0.1:6379/0", decode_responses=True)
    try:
        client.ping()
    except Exception as exc:
        client.close()
        pytest.skip(f"real Redis unavailable: {exc}")
    stream = RedisRuntimeEventStream(client, key_prefix=prefix)
    yield stream, client, prefix
    keys = list(client.scan_iter(f"{prefix}:*"))
    if keys:
        client.delete(*keys)
    client.close()


def _canonical_turn(tmp_path: Path) -> tuple[ClientPaths, SQLiteSessionStore, RuntimeState, NodeFrame]:
    paths = ClientPaths(tmp_path / "data")
    paths.ensure()
    store = SQLiteSessionStore(paths)
    session = store.create_session("events")
    store.create_sidebar_thread(
        session_id=session.session_id,
        thread_id=session.session_id,
        title="events",
    )
    root = store.ensure_root_node(session.session_id, id="turn-root")
    node = RuntimeState.create(
        session_id=session.session_id,
        thread_id=session.session_id,
        id="turn-event",
        parent=root,
        user_content=[{"type": "text", "text": "hello", "status": "success"}],
    )
    frame = NodeFrame.snapshot(node)
    store.create_node_with_frame(node, frame)
    return paths, store, node, frame


def test_real_redis_outbox_publish_is_idempotent_across_relay_race_and_terminal(
    tmp_path: Path,
    redis_event_stream: tuple[RedisRuntimeEventStream, Redis, str],
) -> None:
    stream, client, _prefix = redis_event_stream
    paths, store, node, frame = _canonical_turn(tmp_path)
    state = SimpleNamespace(paths=paths, agent_thread_index=None, runtime_event_stream=stream)
    event = store.runtime_event(node.session_id, frame.event_id)
    assert event is not None

    publish_outbox_event(state, event)
    assert store.runtime_event(node.session_id, frame.event_id) is None
    # Simulate the execution callback losing the race to the relay. The Redis
    # receipt proves publication and must not turn a successful tool into an
    # artificial persistence failure.
    publish_frame(state, frame, node)
    assert client.xlen(stream._turn_key(node.id)) == 1
    assert client.xlen(stream._thread_key(node.thread_id)) == 1

    final = node.clone()
    final.status = "success"
    final.timestamp = utc_iso()
    delta = NodeFrame.delta(node, final, revision=1)
    store.update_node_with_frame(final, delta)
    terminal_event = store.runtime_event(final.session_id, delta.event_id)
    assert terminal_event is not None
    publish_outbox_event(state, terminal_event, recover_terminal=True)
    publish_terminal(
        state,
        session_id=final.session_id,
        thread_id=final.thread_id,
        turn_id=final.id,
        terminal_type="success",
    )

    assert client.xlen(stream._turn_key(final.id)) == 3
    assert client.xlen(stream._thread_key(final.thread_id)) == 3
    ttl = client.ttl(stream._turn_key(final.id))
    assert 0 < ttl <= EVENT_STREAM_TTL_SECONDS
    terminal_entries = [
        fields
        for _entry_id, fields in client.xrange(stream._turn_key(final.id))
        if json.loads(fields["payload"]).get("type") == "turn.terminal"
    ]
    assert len(terminal_entries) == 1


def test_real_redis_event_stream_enforces_exact_maxlen(
    monkeypatch: pytest.MonkeyPatch,
    redis_event_stream: tuple[RedisRuntimeEventStream, Redis, str],
) -> None:
    stream, client, _prefix = redis_event_stream
    monkeypatch.setattr(event_stream_module, "EVENT_STREAM_MAXLEN", 3)
    for sequence in range(1, 6):
        stream.publish(
            event_id=f"event-{sequence}",
            turn_id="turn-maxlen",
            thread_id="thread-maxlen",
            sequence=sequence,
            payload={"type": "turn.delta", "turn_id": "turn-maxlen", "revision": sequence},
        )
    assert client.xlen(stream._turn_key("turn-maxlen")) == 3
    assert client.xlen(stream._thread_key("thread-maxlen")) == 3


def test_sse_reconnect_rebases_from_sqlite_and_emits_standard_cursor_ids(
    tmp_path: Path,
    redis_event_stream: tuple[RedisRuntimeEventStream, Redis, str],
) -> None:
    stream, _client, _prefix = redis_event_stream
    paths, store, node, frame = _canonical_turn(tmp_path)
    state = SimpleNamespace(
        paths=paths,
        agent_thread_index=None,
        runtime_event_stream=stream,
        active_turn_streams={},
    )
    first_event = store.runtime_event(node.session_id, frame.event_id)
    assert first_event is not None
    publish_outbox_event(state, first_event)
    first_cursor = stream.latest_thread_id(node.thread_id)

    final = node.clone()
    final.status = "success"
    final.timestamp = utc_iso()
    delta = NodeFrame.delta(node, final, revision=1)
    store.update_node_with_frame(final, delta)
    second_event = store.runtime_event(final.session_id, delta.event_id)
    assert second_event is not None
    publish_outbox_event(state, second_event)
    publish_terminal(
        state,
        session_id=final.session_id,
        thread_id=final.thread_id,
        turn_id=final.id,
        terminal_type="success",
    )
    latest_cursor = stream.latest_thread_id(final.thread_id)

    async def reconnect() -> list[str]:
        return [
            item
            async for item in turn_sse(
                state,
                final.session_id,
                final.thread_id,
                final.id,
                last_event_id=first_cursor,
            )
        ]

    events = asyncio.run(reconnect())
    assert len(events) == 2
    assert events[0].startswith(f"id: {latest_cursor}\ndata: ")
    assert '"type":"turn.snapshot"' in events[0]
    assert events[1].startswith(f"id: {latest_cursor}\ndata: <SSE")
    assert 'type="success"' in events[1]

    async def thread_baseline() -> list[str]:
        generator = thread_sse(state, final.session_id, final.thread_id)
        try:
            return [await anext(generator), await anext(generator)]
        finally:
            await generator.aclose()

    thread_events = asyncio.run(thread_baseline())
    assert '"type":"thread.ready"' in thread_events[0]
    assert '"type":"turn.snapshot"' in thread_events[1]


def test_turn_sse_waits_for_the_accepted_rewind_delivery_before_using_existing_turn(
    tmp_path: Path,
    redis_event_stream: tuple[RedisRuntimeEventStream, Redis, str],
) -> None:
    stream, _client, _prefix = redis_event_stream
    paths, store, node, _frame = _canonical_turn(tmp_path)
    final = node.clone()
    final.status = "success"
    final.timestamp = utc_iso()
    store.finalize_node(final)
    state = SimpleNamespace(
        paths=paths,
        agent_thread_index=None,
        runtime_event_stream=stream,
        active_turn_streams={},
    )

    async def receive_after_admission() -> str:
        generator = turn_sse(
            state,
            final.session_id,
            final.thread_id,
            final.id,
            delivery_id="rewind-delivery",
        )
        pending = asyncio.create_task(anext(generator))
        await asyncio.sleep(0.1)
        assert not pending.done()
        store.append_turn_version(
            final.id,
            {"type": "text", "text": "rewound", "status": "success"},
            delivery_id="rewind-delivery",
        )
        try:
            return await asyncio.wait_for(pending, timeout=2.0)
        finally:
            await generator.aclose()

    event = asyncio.run(receive_after_admission())
    assert '"type":"turn.snapshot"' in event
    assert '"delivery_id":"rewind-delivery"' in event
    assert "<SSE" not in event
