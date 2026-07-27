from __future__ import annotations

import threading
from pathlib import Path

import pytest

from backend.configuration import ClientPaths, initialize_config, load_config, section
from backend.domain import AssistantMessage, RunState, UserMessage
from backend.planning import RuleBasedPlanner
from backend.runtime import AgentRunner, ConversationService, RuntimeState
from backend.storage.sqlite import SQLiteSessionStore
from backend.sync import RequestsSyncTransport, SyncClient, SyncCoordinator
from backend.tools import ToolRegistry


def test_env_migration_is_atomic_and_device_id_is_stable(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    legacy = workspace / ".env"
    legacy.write_text("API_KEY=secret\nBASE_URL=https://model.test\nMODEL=demo\n", encoding="utf-8")
    paths = ClientPaths(tmp_path / "home" / "mini_agent")

    first = initialize_config(paths, workspace)
    first_device = section(first, "sync")["device_id"]
    second = initialize_config(paths, workspace)

    assert not legacy.exists()
    assert paths.config_file.exists()
    assert section(load_config(paths.config_file), "model")["api_key"] == "secret"
    assert section(second, "sync")["device_id"] == first_device


def test_sqlite_persists_empty_runtime_and_time_zone(tmp_path: Path) -> None:
    store = SQLiteSessionStore(ClientPaths(tmp_path / "mini_agent"), "device_a")
    session = store.create_session("empty")
    state = RuntimeState(session_id=session.session_id, timezone="UTC")

    store.save_runtime(state)

    assert store.load_runtime(session.session_id).timezone == "UTC"
    assert store.pending_sync_operations()[0]["snapshot"]["runtime"]["timezone"] == "UTC"


def test_acknowledgement_rebases_snapshots_created_during_push(tmp_path: Path) -> None:
    store = SQLiteSessionStore(ClientPaths(tmp_path / "rebase"), "device_a")
    session = store.create_session("rebase")
    in_flight = store.pending_sync_operations()[0]
    store.save_runtime(RuntimeState(session_id=session.session_id, timezone="UTC"))

    store.acknowledge_sync_operations([{"operation_id": in_flight["operation_id"], "revision": 1}])

    remaining = store.pending_sync_operations()
    assert len(remaining) == 1
    assert remaining[0]["base_revision"] == 1
    assert remaining[0]["snapshot"]["runtime"]["timezone"] == "UTC"


def test_sync_ack_remote_read_only_and_fork(tmp_path: Path) -> None:
    store = SQLiteSessionStore(ClientPaths(tmp_path / "mini_agent"), "device_a")
    service = ConversationService(AgentRunner(RuleBasedPlanner(), ToolRegistry()), store)
    run = service.run_task("hello", mode="agent")
    operation = store.pending_sync_operations()[0]

    store.acknowledge_sync_operations([{"operation_id": operation["operation_id"], "revision": 1}])
    assert store.remote_revision(service.active_session.session_id) == 1
    assert store.pending_sync_operations() == []

    snapshot = dict(operation["snapshot"])
    snapshot["session"] = {**snapshot["session"], "session_id": "session_remote"}
    store.apply_remote_snapshot(
        {"session_id": "session_remote", "owner_device_id": "device_b", "revision": 2, "snapshot": snapshot},
        local_device_id="device_a",
    )
    with pytest.raises(PermissionError, match="read-only"):
        store.start_turn("session_remote", "run_illegal", "write")

    fork = store.fork_run(run.run_id)
    assert fork.session_id != service.active_session.session_id
    assert store.get_session(fork.session_id) is not None


def test_full_snapshot_round_trip_preserves_history_and_forkability(tmp_path: Path) -> None:
    source = SQLiteSessionStore(ClientPaths(tmp_path / "source"), "device_a")
    service = ConversationService(AgentRunner(RuleBasedPlanner(), ToolRegistry()), source)
    first = service.run_task("first turn", mode="agent")
    second = service.run_task("second turn", mode="agent")
    source_conversation = source.load_conversation(service.active_session.session_id)
    operation = source.pending_sync_operations()[0]
    snapshot = operation["snapshot"]

    assert len(snapshot["session_runs"]) == 2
    assert len(snapshot["runs"]) == 2
    assert snapshot["checkpoints"]
    assert {item["run_id"] for item in snapshot["session_runs"]} == {first.run_id, second.run_id}

    replica = SQLiteSessionStore(ClientPaths(tmp_path / "replica"), "device_b")
    replica.apply_remote_snapshot(
        {
            "session_id": service.active_session.session_id,
            "owner_device_id": "device_a",
            "revision": 1,
            "snapshot": snapshot,
        },
        local_device_id="device_b",
    )

    assert replica.load_conversation(service.active_session.session_id) == source_conversation
    assert {item["run_id"] for item in replica.list_forkable_runs()} == {first.run_id, second.run_id}
    fork = replica.fork_run(first.run_id)
    forked = replica.load_runtime(fork.session_id)
    assert forked is not None
    assert forked.current_run is not None
    assert forked.current_run.run_id != first.run_id
    assert forked.current_run.provenance.source_session_id == service.active_session.session_id
    assert forked.current_run.provenance.source_run_id == first.run_id
    assert replica.load_conversation(fork.session_id)


@pytest.mark.parametrize("status", ["completed", "failed", "cancelled"])
def test_fork_accepts_every_terminal_run_status(tmp_path: Path, status: str) -> None:
    store = SQLiteSessionStore(ClientPaths(tmp_path / status), "device_a")
    session = store.create_session(status)
    run = RunState(task=status, mode="agent", status=status, final_answer=status)
    state = RuntimeState(
        session_id=session.session_id,
        messages=[UserMessage(content=status), AssistantMessage(content=status)],
        current_run=run,
    )
    store.start_turn(session.session_id, run.run_id, run.task)
    store.save_runtime(state)
    store.finish_turn(session.session_id, run.run_id, status, status)

    fork = store.fork_run(run.run_id)

    assert fork.session_id != session.session_id
    assert store.load_conversation(fork.session_id) == [
        {"role": "user", "content": status},
        {"role": "assistant", "content": status},
    ]


def test_fork_rejects_running_run(tmp_path: Path) -> None:
    store = SQLiteSessionStore(ClientPaths(tmp_path / "running"), "device_a")
    session = store.create_session("running")
    run = RunState(task="running", mode="agent")
    store.start_turn(session.session_id, run.run_id, run.task)
    store.save_runtime(RuntimeState(session_id=session.session_id, current_run=run))

    with pytest.raises(ValueError, match="running"):
        store.fork_run(run.run_id)


class RecordingTransport:
    def __init__(self) -> None:
        self.paths: list[str] = []

    def post(self, path: str, payload: dict[str, object]) -> dict[str, object]:
        self.paths.append(path)
        if path.endswith("push"):
            operations = payload["operations"]
            return {
                "acknowledged": [{"operation_id": operation["operation_id"], "revision": 1} for operation in operations]
            }
        return {"sessions": []}


def test_sync_client_pushes_then_pulls_without_polling(tmp_path: Path) -> None:
    store = SQLiteSessionStore(ClientPaths(tmp_path / "mini_agent"), "device_a")
    store.create_session("sync")
    transport = RecordingTransport()

    SyncClient("device_a", transport).synchronize(store)

    assert transport.paths == ["/v1/sync/push", "/v1/sync/pull"]
    assert store.pending_sync_operations() == []


def test_sync_transport_requires_https() -> None:
    with pytest.raises(ValueError, match="HTTPS"):
        RequestsSyncTransport("http://sync.example.test", "token", "device_a")


def test_offline_sync_keeps_the_outbox(tmp_path: Path) -> None:
    store = SQLiteSessionStore(ClientPaths(tmp_path / "offline"), "device_a")
    store.create_session("offline")

    class OfflineTransport:
        def post(self, _path: str, _payload: dict[str, object]) -> dict[str, object]:
            raise ConnectionError("offline")

    with pytest.raises(ConnectionError, match="offline"):
        SyncClient("device_a", OfflineTransport()).synchronize(store)

    assert store.pending_sync_operations()


def test_sync_coordinator_runs_only_when_notified() -> None:
    calls: list[int] = []
    startup = threading.Event()
    checkpoint = threading.Event()
    shutdown = threading.Event()

    class Client:
        def synchronize(self, _store) -> None:
            calls.append(len(calls) + 1)
            (startup, checkpoint, shutdown)[len(calls) - 1].set()

    coordinator = SyncCoordinator(Client(), object())
    coordinator.start()
    assert startup.wait(2)
    assert not checkpoint.wait(0.05)

    coordinator.notify()
    assert checkpoint.wait(2)

    coordinator.close()
    assert shutdown.is_set()
    assert calls == [1, 2, 3]
