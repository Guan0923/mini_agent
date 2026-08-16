from __future__ import annotations

import json
import sqlite3
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


def test_legacy_env_is_not_read_or_deleted_and_device_id_is_stable(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    legacy = workspace / ".env"
    legacy.write_text("API_KEY=secret\nBASE_URL=https://model.test\nMODEL=demo\n", encoding="utf-8")
    paths = ClientPaths(tmp_path / "home" / "mini_agent")

    first = initialize_config(paths, workspace)
    first_device = section(first, "sync")["device_id"]
    second = initialize_config(paths, workspace)

    # Secrets in legacy ``.env`` files are deliberately outside the local
    # client configuration contract.  Initialization must neither consume nor
    # delete them.
    assert legacy.exists()
    assert paths.config_file.exists()
    assert section(load_config(paths.config_file), "model").get("api_key", "") == ""
    assert section(second, "sync")["device_id"] == first_device


def test_sqlite_persists_empty_runtime_and_time_zone(tmp_path: Path) -> None:
    store = SQLiteSessionStore(ClientPaths(tmp_path / "mini_agent"), "device_a")
    session = store.create_session("empty")
    state = RuntimeState(session_id=session.session_id, timezone="UTC")

    store.save_runtime(state)

    assert store.load_runtime(session.session_id).timezone == "UTC"
    assert store.pending_sync_operations()[0]["snapshot"]["runtime"]["timezone"] == "UTC"


def test_sqlite_closes_connection_when_schema_initialization_fails(tmp_path: Path, monkeypatch) -> None:
    class BrokenConnection:
        def __init__(self) -> None:
            self.row_factory = None
            self.rolled_back = False
            self.closed = False

        def execute(self, _statement: str) -> None:
            return None

        def executescript(self, _script: str) -> None:
            raise sqlite3.DatabaseError("broken schema")

        def rollback(self) -> None:
            self.rolled_back = True

        def close(self) -> None:
            self.closed = True

    connection = BrokenConnection()
    monkeypatch.setattr(sqlite3, "connect", lambda _path: connection)
    store = SQLiteSessionStore(ClientPaths(tmp_path / "mini_agent"), "device_a")

    with pytest.raises(sqlite3.DatabaseError, match="broken schema"):
        with store._connection("session_broken"):
            pass

    assert connection.rolled_back is True
    assert connection.closed is True


def test_sqlite_migrates_legacy_metadata_and_outbox(tmp_path: Path) -> None:
    paths = ClientPaths(tmp_path / "mini_agent")
    session_id = "session_legacy"
    database = paths.session_db(session_id)
    database.parent.mkdir(parents=True)
    state = RuntimeState(session_id=session_id, timezone="UTC")
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE session_meta (
                session_id TEXT PRIMARY KEY, title TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE session_runtime (
                session_id TEXT PRIMARY KEY, state_json TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE sync_outbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT, payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )
        connection.execute(
            "INSERT INTO session_meta VALUES (?, ?, ?, ?)",
            (session_id, "legacy", "2025-01-01T00:00:00Z", "2025-01-01T00:00:00Z"),
        )
        connection.execute(
            "INSERT INTO session_runtime VALUES (?, ?, ?)",
            (session_id, json.dumps(state.to_dict()), "2025-01-01T00:00:00Z"),
        )
        connection.execute(
            "INSERT INTO sync_outbox(payload_json,created_at) VALUES (?, ?)",
            ('{"event":"session_changed"}', "2025-01-01T00:00:00Z"),
        )

    store = SQLiteSessionStore(paths, "device_current")

    assert store.load_runtime(session_id).timezone == "UTC"
    operations = store.pending_sync_operations()
    assert len(operations) == 1
    assert operations[0]["operation_id"].startswith("operation_")
    assert operations[0]["snapshot"]["session"]["owner_device_id"] == "device_current"
    assert operations[0]["snapshot"]["runtime"]["timezone"] == "UTC"


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
    snapshot["runtime"] = {**snapshot["runtime"], "session_id": "session_remote"}
    snapshot["nodes"] = [
        {
            **node,
            "session_id": "session_remote",
            "parent_session_id": "session_remote" if node.get("parent_id") else node.get("parent_session_id", ""),
        }
        for node in snapshot.get("nodes", [])
    ]
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


@pytest.mark.parametrize(
    "url",
    [
        "https:///missing-host",
        "https://user:password@sync.example.test",
        "https://sync.example.test?token=unsafe",
        "https://sync.example.test#fragment",
    ],
)
def test_sync_transport_rejects_unsafe_https_endpoints(url: str) -> None:
    with pytest.raises(ValueError, match="sync.url"):
        RequestsSyncTransport(url, "token", "device_a")


def test_sync_transport_rejects_non_https_endpoint() -> None:
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
