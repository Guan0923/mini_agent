from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass
from typing import Any

import pytest
import requests
from backend.cloud import CloudAuthExpired, CloudClient, CloudConflict, CloudUnavailable
from backend.storage.auth.types import UserIdentity
from backend.sync.cloud_repository import CloudSyncConflict as BackendCloudSyncConflict
from cloud.api.app import create_app
from cloud.auth.mail import NullMailer
from cloud.auth.types import AuthStorageUnavailable
from cloud.sync.repository import CloudSyncConflict, EncryptedSnapshotChunk
from fastapi.testclient import TestClient

USER_A = "123e4567-e89b-12d3-a456-426614174000"
USER_B = "123e4567-e89b-12d3-a456-426614174001"


class FakeAuthRepository:
    def __init__(self) -> None:
        self.identities = {
            "token-a": UserIdentity(USER_A, "a@example.com", "account"),
            "token-b": UserIdentity(USER_B, "b@example.com", "account"),
        }
        self.revoked: list[str] = []

    def resolve_token(self, token: str):
        identity = self.identities.get(token)
        return (identity, "device") if identity is not None else None

    def revoke_token(self, token: str) -> None:
        self.revoked.append(token)

    def ping(self) -> None:
        return None

    def close(self) -> None:
        return None


class FakeSnapshotRepository:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.rows = {
            USER_A: [{"id": "snapshot-a", "version": 1}],
            USER_B: [{"id": "snapshot-b", "version": 1}],
        }

    def ping(self) -> None:
        return None

    def list_snapshots(self, user_id: str) -> list[dict[str, object]]:
        self.calls.append(("list", user_id))
        return list(self.rows.get(user_id, []))

    def ensure_user_key(self, user_id: str, dek: bytes) -> None:
        self.calls.append(("key", user_id, dek))

    def recover_user_key(self, user_id: str) -> bytes | None:
        self.calls.append(("recover-key", user_id))
        return None

    def begin_snapshot(self, **kwargs: object) -> int:
        self.calls.append(("begin", kwargs["user_id"], kwargs))
        if kwargs["snapshot_id"] == "conflict":
            raise CloudSyncConflict("head changed")
        return 7

    def append_chunk(self, user_id: str, snapshot_id: str, chunk: EncryptedSnapshotChunk) -> None:
        self.calls.append(("chunk", user_id, snapshot_id, chunk))

    def complete_snapshot(self, user_id: str, snapshot_id: str, **kwargs: object) -> None:
        self.calls.append(("complete", user_id, snapshot_id, kwargs))

    def fail_snapshot(self, user_id: str, snapshot_id: str) -> None:
        self.calls.append(("fail", user_id, snapshot_id))

    def download(self, user_id: str, snapshot_id: str):
        self.calls.append(("download", user_id, snapshot_id))
        if snapshot_id != "snapshot-a":
            raise ValueError("not found")
        ciphertext = b"c" * 16
        chunk = EncryptedSnapshotChunk(0, b"n" * 12, ciphertext, hashlib.sha256(ciphertext).hexdigest())
        return {"id": snapshot_id, "version": 1}, [chunk]

    def close(self) -> None:
        return None


@pytest.fixture
def cloud_repositories() -> tuple[FakeAuthRepository, FakeSnapshotRepository]:
    return FakeAuthRepository(), FakeSnapshotRepository()


def test_cloud_routes_derive_snapshot_owner_from_bearer_token(cloud_repositories) -> None:
    auth, snapshots = cloud_repositories
    app = create_app(auth_repository=auth, snapshot_repository=snapshots, mailer=NullMailer())
    with TestClient(app) as client:
        response = client.get("/v1/sync/snapshots", headers={"Authorization": "Bearer token-a"})
        assert response.status_code == 200
        assert response.json() == [{"id": "snapshot-a", "version": 1}]

        # A client-supplied user_id is ignored; only the authenticated token
        # determines ownership of a write.
        response = client.post(
            "/v1/sync/snapshots/begin",
            headers={"Authorization": "Bearer token-a"},
            json={
                "snapshot_id": "snapshot-new",
                "parent_snapshot_id": None,
                "local_revision": 3,
                "device_id": "device-a",
                "user_id": USER_B,
            },
        )
        assert response.status_code == 200
        begin = next(call for call in snapshots.calls if call[0] == "begin")
        assert begin[1] == USER_A

        assert client.get("/v1/sync/snapshots", headers={"Authorization": "Bearer token-b"}).json() == [
            {"id": "snapshot-b", "version": 1}
        ]
        assert client.get("/v1/sync/snapshots").status_code == 401


def test_cloud_routes_validate_chunks_and_map_conflicts(cloud_repositories) -> None:
    auth, snapshots = cloud_repositories
    app = create_app(auth_repository=auth, snapshot_repository=snapshots, mailer=NullMailer())
    ciphertext = b"x" * 16
    encoded = base64.urlsafe_b64encode(ciphertext).decode("ascii")
    nonce = base64.urlsafe_b64encode(b"n" * 12).decode("ascii")
    headers = {"Authorization": "Bearer token-a"}
    with TestClient(app) as client:
        invalid = client.put(
            "/v1/sync/snapshots/snapshot-new/chunks/0",
            headers=headers,
            json={"nonce": nonce, "ciphertext": encoded, "checksum": "0" * 64},
        )
        assert invalid.status_code == 422
        assert not any(call[0] == "chunk" for call in snapshots.calls)

        valid = client.put(
            "/v1/sync/snapshots/snapshot-new/chunks/0",
            headers=headers,
            json={"nonce": nonce, "ciphertext": encoded, "checksum": hashlib.sha256(ciphertext).hexdigest()},
        )
        assert valid.status_code == 200
        chunk = next(call for call in snapshots.calls if call[0] == "chunk")
        assert chunk[1] == USER_A
        assert chunk[3].ciphertext == ciphertext

        conflict = client.post(
            "/v1/sync/snapshots/begin",
            headers=headers,
            json={
                "snapshot_id": "conflict",
                "parent_snapshot_id": "stale",
                "local_revision": 4,
                "device_id": "device-a",
            },
        )
        assert conflict.status_code == 409
        assert conflict.json()["detail"] == "head changed"

        downloaded = client.get("/v1/sync/snapshots/snapshot-a", headers=headers)
        assert downloaded.status_code == 200
        assert downloaded.json()["chunks"][0]["checksum"] == hashlib.sha256(b"c" * 16).hexdigest()


def test_cloud_client_maps_network_auth_and_conflict_errors() -> None:
    @dataclass
    class Response:
        status_code: int
        payload: object

        content: bytes = b"{}"
        headers: dict[str, str] | None = None

        def json(self):
            return self.payload

    class Session:
        def __init__(self, response: Response | None = None, error: Exception | None = None) -> None:
            self.response = response
            self.error = error
            self.calls: list[dict[str, object]] = []

        def request(self, method: str, url: str, **kwargs: object):
            self.calls.append({"method": method, "url": url, **kwargs})
            if self.error is not None:
                raise self.error
            assert self.response is not None
            return self.response

        def close(self) -> None:
            return None

    network = Session(error=requests.ConnectionError("offline"))
    client = CloudClient("https://cloud.example", token="secret", session=network, timeout=3)
    with pytest.raises(CloudUnavailable) as network_error:
        client.me()
    assert network_error.value.retryable is True
    client.close()

    cleared: list[bool] = []
    unauthorized = Session(Response(401, {"detail": "expired"}))
    client = CloudClient(
        "https://cloud.example",
        token="secret",
        session=unauthorized,
        on_auth_expired=lambda: cleared.append(True),
    )
    with pytest.raises(CloudAuthExpired) as auth_error:
        client.me()
    assert auth_error.value.status_code == 401
    assert cleared == [True]
    assert unauthorized.calls[0]["verify"] is True

    conflict = Session(Response(409, {"detail": "head changed"}))
    with pytest.raises(CloudConflict) as conflict_error:
        CloudClient("https://cloud.example", token="secret", session=conflict).me()
    assert isinstance(conflict_error.value, BackendCloudSyncConflict)
    assert conflict_error.value.status_code == 409


def test_cloud_client_rejects_insecure_or_ambiguous_urls() -> None:
    with pytest.raises(ValueError):
        CloudClient("http://cloud.example")
    with pytest.raises(ValueError):
        CloudClient("https://cloud.example/?token=secret")
    assert CloudClient("http://127.0.0.1:8100").base_url == "http://127.0.0.1:8100"


def test_cloud_openapi_exposes_only_versioned_control_plane_paths(cloud_repositories) -> None:
    auth, snapshots = cloud_repositories
    app = create_app(auth_repository=auth, snapshot_repository=snapshots, mailer=NullMailer())
    paths = set(app.openapi()["paths"])
    assert "/health" in paths
    assert "/ready" in paths
    assert "/v1/auth/login" in paths
    assert "/v1/auth/me" in paths
    assert "/v1/devices/token" in paths
    assert "/v1/sync/keys" in paths
    assert "/v1/sync/snapshots" in paths
    assert "/v1/sync/snapshots/{snapshot_id}/chunks/{sequence}" in paths
    assert not any(path.startswith("/api/") for path in paths)


def test_cloud_ready_reports_storage_failure() -> None:
    class BrokenAuth(FakeAuthRepository):
        def ping(self) -> None:
            raise AuthStorageUnavailable("database offline")

    app = create_app(auth_repository=BrokenAuth(), snapshot_repository=FakeSnapshotRepository(), mailer=NullMailer())
    with TestClient(app) as client:
        response = client.get("/ready")
    assert response.status_code == 503
    assert "数据库" in response.json()["detail"]
