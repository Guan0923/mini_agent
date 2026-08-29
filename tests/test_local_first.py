from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from backend.configuration import ClientPaths, initialize_config, load_config, section
from backend.runtime import RuntimeState
from backend.storage.sqlite import SQLiteSessionStore
from backend.sync import RequestsSyncTransport, SyncClient, SyncCoordinator


def test_legacy_env_is_not_read_or_deleted_and_device_id_is_stable(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    legacy = workspace / ".env"
    legacy.write_text("API_KEY=secret\nBASE_URL=https://model.test\nMODEL=demo\n", encoding="utf-8")
    paths = ClientPaths(tmp_path / "home" / "mini_agent")

    first = initialize_config(paths, workspace)
    first_device = section(first, "sync")["device_id"]
    second = initialize_config(paths, workspace)

    assert legacy.exists()
    assert paths.config_file.exists()
    assert section(load_config(paths.config_file), "model").get("api_key", "") == ""
    assert section(second, "sync")["device_id"] == first_device


def test_sqlite_persists_empty_runtime_and_time_zone_as_v10_event(tmp_path: Path) -> None:
    store = SQLiteSessionStore(ClientPaths(tmp_path / "mini_agent"), "device_a")
    session = store.create_session("empty")
    state = RuntimeState(session_id=session.session_id, timezone="UTC")

    store.save_runtime(state)

    assert store.load_runtime(session.session_id).timezone == "UTC"
    events = store.pending_sync_operations()[0]["events"]
    saved = next(event for event in events if event["kind"] == "runtime_state_saved")
    assert saved["payload"]["state"]["timezone"] == "UTC"


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


class RecordingTransport:
    def __init__(self) -> None:
        self.paths: list[str] = []

    def post(self, path: str, payload: dict[str, object]) -> dict[str, object]:
        del payload
        self.paths.append(path)
        return {"revision": 1}

    def get(self, path: str) -> dict[str, object]:
        self.paths.append(path.split("?", 1)[0])
        return {"events": []}


def test_sync_client_pushes_then_pulls_without_polling(tmp_path: Path) -> None:
    store = SQLiteSessionStore(ClientPaths(tmp_path / "mini_agent"), "device_a")
    store.create_session("sync")
    transport = RecordingTransport()

    SyncClient("device_a", transport, key_provider=lambda _session_id: b"k" * 32).synchronize(store)

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
        SyncClient("device_a", OfflineTransport(), key_provider=lambda _session_id: b"k" * 32).synchronize(store)

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
