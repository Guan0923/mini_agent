from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from backend.sync.server import (
    PostgresSyncRepository,
    RevisionConflict,
    SessionOwnershipError,
    create_sync_app,
)


def _operation(
    operation_id: str,
    session_id: str = "session_sync",
    base_revision: int = 0,
) -> dict[str, object]:
    return {
        "operation_id": operation_id,
        "session_id": session_id,
        "base_revision": base_revision,
        "kind": "snapshot",
        "snapshot": {"session": {"session_id": session_id}, "runtime": None},
    }


def test_postgres_sync_repository_enforces_idempotency_revision_and_owner() -> None:
    database_url = os.environ["TEST_DATABASE_URL"].replace("@localhost:", "@127.0.0.1:")
    repository = PostgresSyncRepository(database_url)
    first = _operation("operation_one")

    assert repository.push("device_a", [first]) == [{"operation_id": "operation_one", "revision": 1}]
    assert repository.push("device_a", [first]) == [{"operation_id": "operation_one", "revision": 1}]
    pulled = repository.pull({})
    assert pulled[0]["owner_device_id"] == "device_a"
    assert pulled[0]["revision"] == 1
    assert repository.pull({"session_sync": 1}) == []

    assert repository.push("device_a", [_operation("operation_two", base_revision=1)]) == [
        {"operation_id": "operation_two", "revision": 2}
    ]
    with pytest.raises(RevisionConflict):
        repository.push("device_a", [_operation("operation_stale", base_revision=1)])
    with pytest.raises(SessionOwnershipError):
        repository.push("device_b", [_operation("operation_foreign", base_revision=2)])
    with pytest.raises(ValueError, match="another session"):
        repository.push("device_a", [_operation("operation_one", "session_other")])


def test_postgres_sync_repository_serializes_concurrent_duplicate_operation() -> None:
    database_url = os.environ["TEST_DATABASE_URL"].replace("@localhost:", "@127.0.0.1:")
    repository = PostgresSyncRepository(database_url)
    session_id = "session_concurrent"
    repository.push("device_a", [_operation("operation_initial", session_id)])
    duplicate = _operation("operation_duplicate", session_id, base_revision=1)
    ready = threading.Barrier(2)

    def push() -> list[dict[str, object]]:
        ready.wait(timeout=5)
        return repository.push("device_a", [duplicate])

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = [future.result(timeout=10) for future in (executor.submit(push), executor.submit(push))]

    assert results == [
        [{"operation_id": "operation_duplicate", "revision": 2}],
        [{"operation_id": "operation_duplicate", "revision": 2}],
    ]


def test_sync_api_authenticates_and_passes_device_identity() -> None:
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient

    class Repository:
        def __init__(self) -> None:
            self.device_id: str | None = None

        def push(self, device_id: str, operations: list[dict[str, object]]) -> list[dict[str, object]]:
            self.device_id = device_id
            return [{"operation_id": operations[0]["operation_id"], "revision": 1}]

        def pull(self, _known: dict[str, object]) -> list[dict[str, object]]:
            return []

    repository = Repository()
    client = TestClient(create_sync_app(repository, "server-secret"))
    operation = _operation("operation_api")

    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.post("/v1/sync/push", json={"operations": [operation]}).status_code == 401
    response = client.post(
        "/v1/sync/push",
        json={"operations": [operation]},
        headers={"Authorization": "Bearer server-secret", "X-Device-ID": "device_api"},
    )
    assert response.status_code == 200
    assert response.json() == {"acknowledged": [{"operation_id": "operation_api", "revision": 1}]}
    assert repository.device_id == "device_api"


def test_sync_api_maps_conflicts_without_leaking_snapshot_content() -> None:
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient

    class Repository:
        def push(self, _device_id: str, _operations: list[dict[str, object]]) -> list[dict[str, object]]:
            raise RevisionConflict("private-session-content")

        def pull(self, _known: dict[str, object]) -> list[dict[str, object]]:
            return []

    client = TestClient(create_sync_app(Repository(), "server-secret"))
    response = client.post(
        "/v1/sync/push",
        json={"operations": [_operation("operation_conflict")]},
        headers={"Authorization": "Bearer server-secret", "X-Device-ID": "device_a"},
    )
    assert response.status_code == 409
    assert response.json() == {"detail": "Revision conflict"}
    assert "private-session-content" not in response.text
