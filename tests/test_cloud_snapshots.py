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
        assert (
            restored_paths.session_uploads("session_1") / "old.png"
        ).read_bytes() == b"\x89PNG\r\n\x1a\nlegacy-upload"
        assert not (restored_paths.session_root("session_1") / "uploads").exists()
    finally:
        manager.close()


def test_old_format_snapshot_archive_uploads_migrate_on_restore(tmp_path: Path, monkeypatch) -> None:
    """A hand-built v2 archive with the pre-canonical uploads path restores and migrates."""

    import hashlib
    import io
    import secrets
    import zipfile

    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    from backend.sync.cloud_repository import EncryptedSnapshotChunk
    from backend.sync.snapshots import CHUNK_SIZE, SNAPSHOT_FORMAT_VERSION

    keys = MemoryKeyStore()
    monkeypatch.setattr(crypto, "_LOCAL_KEY_STORE", keys)
    settings = PerUserSettingsRepository(tmp_path)
    settings.update_profile(USER_ID, display_name="User", agent_preferences="")

    # Build the legacy snapshot payload: runtime/session_1/{state.db,uploads/old.png}.
    payload = tmp_path / "legacy-payload"
    session_payload = payload / "runtime" / "session_1"
    (session_payload / "workspace").mkdir(parents=True)
    (session_payload / "uploads").mkdir()
    (session_payload / "uploads" / "old.png").write_bytes(b"\x89PNG\r\nlegacy-archive")
    state_db = session_payload / "state.db"
    with sqlite3.connect(state_db) as connection:
        connection.execute("CREATE TABLE session_runs (status TEXT)")
        connection.commit()
    (payload / "config.toml").write_text("[runtime]\nlog_full_messages = true\n", encoding="utf-8")
    user_db = payload / "user.db"
    with sqlite3.connect(user_db) as connection:
        connection.execute("CREATE TABLE user_profile (id TEXT PRIMARY KEY, display_name TEXT)")
        connection.commit()

    files = [
        {
            "path": path.relative_to(payload).as_posix(),
            "size": path.stat().st_size,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
        for path in sorted(payload.rglob("*"))
        if path.is_file()
    ]
    (payload / "manifest.json").write_text(
        json.dumps(
            {
                "format_version": SNAPSHOT_FORMAT_VERSION,
                "user_id": USER_ID,
                "local_revision": 1,
                "created_at": time.time(),
                "files": files,
            }
        ),
        encoding="utf-8",
    )
    archive_bytes = io.BytesIO()
    with zipfile.ZipFile(archive_bytes, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(payload.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(payload).as_posix())

    key = b"k" * 32
    keys.set(USER_ID, key)
    snapshot_id = "snapshot_legacy_old_layout"
    cipher = AESGCM(key)
    chunks: list[EncryptedSnapshotChunk] = []
    plaintext = archive_bytes.getvalue()
    for sequence in range(0, max(len(plaintext), 1), CHUNK_SIZE):
        nonce = secrets.token_bytes(12)
        index = sequence // CHUNK_SIZE
        aad = f"mini-agent-snapshot:v2:{USER_ID}:{snapshot_id}:{index}".encode()
        ciphertext = cipher.encrypt(nonce, plaintext[sequence : sequence + CHUNK_SIZE], aad)
        chunks.append(
            EncryptedSnapshotChunk(
                index,
                nonce,
                ciphertext,
                hashlib.sha256(ciphertext).hexdigest(),
            )
        )
    cloud = MemoryCloudRepository()
    cloud.chunks[snapshot_id] = chunks
    cloud.snapshots[snapshot_id] = {
        "id": snapshot_id,
        "version": 1,
        "local_revision": 1,
        "device_id": "web-test",
        "archive_size": len(plaintext),
        "archive_sha256": hashlib.sha256(plaintext).hexdigest(),
        "chunk_count": len(chunks),
        "completed_at": "2026-08-09T00:00:00",
    }
    cloud.key = key

    manager = SnapshotManager(tmp_path, settings, cloud)  # type: ignore[arg-type]
    manager.key_store = keys
    try:
        restore = manager.start_restore(USER_ID, snapshot_id)
        restored = _wait_for_job(manager, USER_ID, str(restore["id"]))
        assert restored["status"] == "complete", restored.get("error")

        paths = ClientPaths(tmp_path / USER_ID)
        assert (paths.session_uploads("session_1") / "old.png").read_bytes() == b"\x89PNG\r\nlegacy-archive"
        assert not (paths.session_root("session_1") / "uploads").exists()
    finally:
        manager.close()
