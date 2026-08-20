"""Focused acceptance tests for the v9 local JSON event protocol."""

from __future__ import annotations

import json
import sqlite3

import pytest

from backend.configuration import ClientPaths
from backend.domain import RunState, RuntimeMessage, UserMessage
from backend.domain.runtime_state import NodeWriter, message_payload
from backend.runtime.core.context import RuntimeState
from backend.storage.sqlite import SQLiteSessionStore
from backend.sync.events import decrypt_event_batch, encrypt_event_batch


def _store(root, device: str) -> SQLiteSessionStore:
    return SQLiteSessionStore(ClientPaths(root), device)


def test_local_store_uses_json_objects_and_small_immutable_events(tmp_path) -> None:
    store = _store(tmp_path / "one", "device-a")
    session = store.create_session()
    writer = NodeWriter(store)
    parent = store.get_session_root(session.session_id)
    assert parent is not None
    for index in range(4):
        node = writer.create(
            session_id=session.session_id,
            parent=parent,
            data=message_payload("user", f"question {index}"),
        )
        parent = writer.delete(node.session_id, node.id)
    store.save_runtime(RuntimeState(session_id=session.session_id, messages=[UserMessage(content="cached")]))
    store.start_turn(session.session_id, "run-1", "task")
    message = RuntimeMessage(1, "progress", "working", "2026-01-01T00:00:00Z", {})
    store.append_runtime_message(session.session_id, "run-1", message)
    store.append_runtime_message(session.session_id, "run-1", message)

    with sqlite3.connect(store.paths.session_db(session.session_id)) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "sync_outbox" not in tables
        assert "session_messages" not in tables
        events = connection.execute("SELECT kind,payload_json FROM json_events ORDER BY local_sequence").fetchall()
    assert len(events) >= 2 + 2 * 4
    state_events = [json.loads(payload) for kind, payload in events if kind == "runtime_state_saved"]
    assert state_events and "messages" not in state_events[-1]["state"]
    runtime_message_events = [json.loads(payload) for kind, payload in events if kind == "runtime_message_appended"]
    assert sum(item.get("kind") == "progress" for item in runtime_message_events) == 1
    assert max(len(payload) for _kind, payload in events) < 20_000

    restarted = _store(tmp_path / "one", "device-a")
    assert restarted.load_conversation(session.session_id) == [
        {"role": "user", "content": f"question {index}"} for index in range(4)
    ]
    assert restarted.load_runtime(session.session_id) is not None


def test_baseline_delta_replay_is_ordered_and_idempotent(tmp_path) -> None:
    source = _store(tmp_path / "source", "source-device")
    session = source.create_session("Sync me")
    writer = NodeWriter(source)
    parent = source.get_session_root(session.session_id)
    assert parent is not None
    node = writer.create(session_id=session.session_id, parent=parent, data=message_payload("user", "hello"))
    writer.delete(node.session_id, node.id)
    first = source.pending_sync_operations()[0]

    target = _store(tmp_path / "target", "target-device")
    target.apply_sync_events(
        {
            "session_id": session.session_id,
            "revision": 1,
            "parent_revision": 0,
            "owner_device_id": "source-device",
            "events": first["events"],
        },
        local_device_id="target-device",
    )
    assert target.load_conversation(session.session_id) == [{"role": "user", "content": "hello"}]
    assert (
        target.apply_sync_events(
            {
                "session_id": session.session_id,
                "revision": 1,
                "parent_revision": 0,
                "events": first["events"],
            },
            local_device_id="target-device",
        )
        == 1
    )

    source.acknowledge_sync_operations(
        [{"session_id": session.session_id, "event_ids": [item["event_id"] for item in first["events"]], "revision": 1}]
    )
    parent = max(source.load_nodes(session.session_id), key=lambda item: (item.timestamp, item.id))
    node = writer.create(session_id=session.session_id, parent=parent, data=message_payload("assistant", "world"))
    writer.delete(node.session_id, node.id)
    delta = source.pending_sync_operations()[0]
    target.apply_sync_events(
        {
            "session_id": session.session_id,
            "revision": 2,
            "parent_revision": 1,
            "owner_device_id": "source-device",
            "events": delta["events"],
        },
        local_device_id="target-device",
    )
    assert target.load_conversation(session.session_id)[-1] == {"role": "assistant", "content": "world"}

    bad = dict(delta["events"][0])
    bad["checksum"] = "0" * 64
    with pytest.raises(ValueError, match="checksum"):
        target.apply_sync_events(
            {
                "session_id": session.session_id,
                "revision": 3,
                "parent_revision": 2,
                "events": [bad],
            },
            local_device_id="target-device",
        )


def test_runtime_state_history_replays_as_small_deltas(tmp_path) -> None:
    source = _store(tmp_path / "source", "source-device")
    session = source.create_session("state")
    run = RunState(task="task", mode="agent", run_id="run-1")
    run.history.append(UserMessage(content="first"))
    source.save_runtime(RuntimeState(session_id=session.session_id, current_run=run))
    first = source.pending_sync_operations()[0]

    target = _store(tmp_path / "target", "target-device")
    target.apply_sync_events(
        {"session_id": session.session_id, "revision": 1, "parent_revision": 0, "events": first["events"]},
        local_device_id="target-device",
    )
    restored = target.load_runtime(session.session_id)
    assert restored is not None and restored.current_run is not None
    assert [item.content for item in restored.current_run.history] == ["first"]

    source.acknowledge_sync_operations(
        [{"session_id": session.session_id, "event_ids": [item["event_id"] for item in first["events"]], "revision": 1}]
    )
    run.history.append(UserMessage(content="second"))
    source.save_runtime(RuntimeState(session_id=session.session_id, current_run=run))
    delta = source.pending_sync_operations()[0]
    delta_payloads = [item["payload"] for item in delta["events"] if item["kind"] == "runtime_state_delta"]
    assert delta_payloads and len(delta_payloads[-1].get("history_values", [])) == 1
    target.apply_sync_events(
        {"session_id": session.session_id, "revision": 2, "parent_revision": 1, "events": delta["events"]},
        local_device_id="target-device",
    )
    restored = target.load_runtime(session.session_id)
    assert restored is not None and restored.current_run is not None
    assert [item.content for item in restored.current_run.history] == ["first", "second"]


def test_encrypted_events_hide_plaintext_and_reject_wrong_key() -> None:
    events = [{"event_id": "event-1", "kind": "runtime_message_appended", "payload": {"message": "secret"}}]
    key = b"k" * 32
    envelope = encrypt_event_batch(events, key, aad="session-1")
    assert "secret" not in json.dumps(envelope)
    assert decrypt_event_batch(envelope, key, aad="session-1") == events
    with pytest.raises(ValueError, match="decryption"):
        decrypt_event_batch(envelope, b"x" * 32, aad="session-1")
