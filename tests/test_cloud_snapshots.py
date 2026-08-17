from __future__ import annotations

import json
import shutil
import sqlite3
import time
from pathlib import Path

from backend.configuration import ClientPaths
from backend.storage.auth import crypto
from backend.storage.user_settings import PerUserSettingsRepository
from backend.sync.cloud_repository import EncryptedSnapshotChunk
from backend.sync.snapshots import SnapshotManager


class MemoryKeyStore:
    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}

    def get(self, user_id: str) -> bytes | None:
        return self.values.get(user_id)

    def set(self, user_id: str, key: bytes) -> None:
        self.values[user_id] = key

    def get_or_create(self, user_id: str) -> bytes:
        return self.values.setdefault(user_id, b"k" * 32)


class MemoryCloudRepository:
    def __init__(self) -> None:
        self.key: bytes | None = None
        self.chunks: dict[str, list[EncryptedSnapshotChunk]] = {}
        self.snapshots: dict[str, dict[str, object]] = {}

    def ensure_user_key(self, _user_id: str, dek: bytes) -> None:
        self.key = self.key or dek

    def recover_user_key(self, _user_id: str) -> bytes | None:
        return self.key

    def begin_snapshot(self, *, snapshot_id: str, local_revision: int, device_id: str, **_kwargs) -> int:
        self.chunks[snapshot_id] = []
        self.snapshots[snapshot_id] = {
            "id": snapshot_id,
            "version": len(self.snapshots) + 1,
            "local_revision": local_revision,
            "device_id": device_id,
            "archive_size": 0,
            "chunk_count": 0,
            "completed_at": "2026-08-09T00:00:00",
        }
        return int(self.snapshots[snapshot_id]["version"])

    def append_chunk(self, snapshot_id: str, chunk: EncryptedSnapshotChunk) -> None:
        self.chunks[snapshot_id].append(chunk)

    def complete_snapshot(
        self,
        _user_id: str,
        snapshot_id: str,
        *,
        archive_sha256: str,
        archive_size: int,
        chunk_count: int,
    ) -> None:
        self.snapshots[snapshot_id].update(
            archive_sha256=archive_sha256,
            archive_size=archive_size,
            chunk_count=chunk_count,
        )

    def fail_snapshot(self, _user_id: str, _snapshot_id: str) -> None:
        raise AssertionError("snapshot unexpectedly failed")

    def list_snapshots(self, _user_id: str) -> list[dict[str, object]]:
        return list(self.snapshots.values())[-3:][::-1]

    def download(self, _user_id: str, snapshot_id: str):
        return self.snapshots[snapshot_id], self.chunks[snapshot_id]


USER_ID = "123e4567-e89b-12d3-a456-426614174000"


def _wait_for_job(manager: SnapshotManager, user_id: str, job_id: str) -> dict[str, object]:
    deadline = time.time() + 10
    while time.time() < deadline:
        job = manager.job(user_id, job_id)
        assert job is not None
        if job["status"] not in {"queued", "running"}:
            return job
        time.sleep(0.02)
    raise AssertionError("snapshot job did not finish")


def test_user_settings_use_per_user_db_and_authenticated_encryption(tmp_path: Path, monkeypatch) -> None:
    keys = MemoryKeyStore()
    monkeypatch.setattr(crypto, "_LOCAL_KEY_STORE", keys)
    repository = PerUserSettingsRepository(tmp_path)

    provider = repository.add_provider_config(
        USER_ID,
        {
            "provider": "openai",
            "protocol": "chat_completions",
            "base_url": "https://example.test/v1",
            "model": "model-1",
            "api_key": "top-secret-value",
            "max_tokens": 100,
            "context_size": 1000,
            "tokenizer_model": "model-1",
        },
    )
    database = tmp_path / USER_ID / "user.db"
    assert database.exists()
    with sqlite3.connect(database) as connection:
        raw = str(connection.execute("SELECT provider_configs_json FROM user_provider_settings").fetchone()[0])
        ciphertext = str(json.loads(raw)[0]["api_key_ciphertext"])
    assert ciphertext.startswith("v3:")
    assert "top-secret-value" not in ciphertext
    discovered = repository.provider_config_for_discovery(USER_ID, str(provider["id"]))
    assert discovered is not None and discovered["api_key"] == "top-secret-value"


def test_snapshot_round_trip_restores_session_workspace(tmp_path: Path, monkeypatch) -> None:
    keys = MemoryKeyStore()
    monkeypatch.setattr(crypto, "_LOCAL_KEY_STORE", keys)
    settings = PerUserSettingsRepository(tmp_path)
    settings.update_profile(USER_ID, display_name="User", agent_preferences="")
    paths = ClientPaths(tmp_path / USER_ID)
    paths.ensure_session("session_1")
    connection = sqlite3.connect(paths.session_db("session_1"))
    try:
        connection.execute("CREATE TABLE session_runs (status TEXT)")
        connection.commit()
    finally:
        connection.close()
    note = paths.session_workspace("session_1") / "note.txt"
    note.write_text("snapshot content", encoding="utf-8")

    cloud = MemoryCloudRepository()
    manager = SnapshotManager(tmp_path, settings, cloud)  # type: ignore[arg-type]
    manager.key_store = keys
    try:
        save = manager.start_save(USER_ID)
        saved = _wait_for_job(manager, USER_ID, str(save["id"]))
        assert saved["status"] == "complete", saved
        snapshot_id = str(saved["snapshot_id"])
        note.unlink()

        restore = manager.start_restore(USER_ID, snapshot_id)
        restored = _wait_for_job(manager, USER_ID, str(restore["id"]))
        assert restored["status"] == "complete", restored.get("error")
        assert note.read_text(encoding="utf-8") == "snapshot content"
        assert settings.profile_for_user(USER_ID)["display_name"] == "User"
    finally:
        manager.close()


def test_legacy_snapshot_uploads_migrate_on_restore(tmp_path: Path, monkeypatch) -> None:
    """Old snapshots carrying runtime/<sid>/uploads auto-migrate into workspace/uploads."""

    keys = MemoryKeyStore()
    monkeypatch.setattr(crypto, "_LOCAL_KEY_STORE", keys)
    settings = PerUserSettingsRepository(tmp_path)
    settings.update_profile(USER_ID, display_name="User", agent_preferences="")
    paths = ClientPaths(tmp_path / USER_ID)
    paths.ensure_session("session_1")
    connection = sqlite3.connect(paths.session_db("session_1"))
    try:
        connection.execute("CREATE TABLE session_runs (status TEXT)")
        connection.commit()
    finally:
        connection.close()
    # Simulate a pre-canonical layout: uploads next to the workspace.
    legacy = paths.session_root("session_1") / "uploads"
    legacy.mkdir()
    (legacy / "old.png").write_bytes(b"\x89PNG\r\n\x1a\nlegacy-upload")

    cloud = MemoryCloudRepository()
    manager = SnapshotManager(tmp_path, settings, cloud)  # type: ignore[arg-type]
    manager.key_store = keys
    try:
        save = manager.start_save(USER_ID)
        saved = _wait_for_job(manager, USER_ID, str(save["id"]))
        assert saved["status"] == "complete", saved
        snapshot_id = str(saved["snapshot_id"])

        # Wipe the local session and restore from the legacy snapshot.
        shutil.rmtree(paths.session_root("session_1"))
        restore = manager.start_restore(USER_ID, snapshot_id)
        restored = _wait_for_job(manager, USER_ID, str(restore["id"]))
        assert restored["status"] == "complete", restored.get("error")

        restored_paths = ClientPaths(tmp_path / USER_ID)
        restored_paths.ensure_session("session_1")
        assert (restored_paths.session_uploads("session_1") / "old.png").read_bytes() == b"\x89PNG\r\n\x1a\nlegacy-upload"
        assert not (restored_paths.session_root("session_1") / "uploads").exists()
    finally:
        manager.close()
