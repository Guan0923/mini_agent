"""Background creation, encryption, upload, and restoration of user snapshots."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import shutil
import sqlite3
import threading
import time
import zipfile
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath
from uuid import uuid4

from backend.configuration import ClientPaths, validate_identity_id
from backend.storage.auth.crypto import UserDataKeyStore

from .cloud_repository import (
    CloudSyncConflict,
    EncryptedSnapshotChunk,
)
from .ports import SnapshotRepository

SNAPSHOT_FORMAT_VERSION = 2
CHUNK_SIZE = 1024 * 1024
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_SNAPSHOT_ROOTS = frozenset({"skills", "rag", "plugins", "mcp"})
_SNAPSHOT_EXCLUDED_PARTS = frozenset(
    {
        "sync",
        "benchmark",
        "cache",
        ".cache",
        "tmp",
        ".tmp",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".vite",
        "node_modules",
        "logs",
        "log",
    }
)
_SNAPSHOT_EXCLUDED_SUFFIXES = (".jsonl", ".log", ".tmp", ".cache")


@dataclass
class SnapshotJob:
    id: str
    user_id: str
    kind: str
    status: str = "queued"
    phase: str = "queued"
    progress: int = 0
    snapshot_id: str | None = None
    error: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def public(self) -> dict[str, object]:
        value = asdict(self)
        value.pop("user_id", None)
        return value


class SnapshotManager:
    """Own bounded background jobs and serialize snapshot mutations per user."""

    def __init__(
        self,
        data_root: Path,
        settings,
        repository: SnapshotRepository,
        *,
        user_allowed: Callable[[str], bool] | None = None,
    ) -> None:
        data_root = Path(data_root)
        if data_root.is_symlink():
            raise ValueError("Snapshot data root cannot be a symbolic link.")
        self.data_root = data_root.resolve()
        self.data_root.mkdir(parents=True, exist_ok=True)
        self.settings = settings
        self.repository = repository
        self._user_allowed = user_allowed or (lambda _user_id: True)
        self.key_store = UserDataKeyStore()
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="mini-agent-cloud")
        self._jobs: dict[str, SnapshotJob] = {}
        self._jobs_lock = threading.Lock()
        self._user_locks: dict[str, threading.Lock] = {}
        self._last_auto_attempt: dict[str, float] = {}
        self._stop = threading.Event()
        self._scheduler = threading.Thread(
            target=self._schedule_loop,
            name="mini-agent-auto-save",
            daemon=True,
        )
        self._scheduler.start()

    def _device_id_for(self, user_id: str) -> str:
        getter = getattr(self.settings, "device_id_for_user", None)
        if callable(getter):
            return str(getter(user_id))
        return f"device_{user_id}"

    def _lock_for(self, user_id: str) -> threading.Lock:
        with self._jobs_lock:
            return self._user_locks.setdefault(user_id, threading.Lock())

    def _new_job(self, user_id: str, kind: str) -> SnapshotJob:
        with self._jobs_lock:
            active = next(
                (job for job in self._jobs.values() if job.user_id == user_id and job.status in {"queued", "running"}),
                None,
            )
            if active is not None:
                return active
            job = SnapshotJob(f"syncjob_{uuid4().hex}", user_id, kind)
            self._jobs[job.id] = job
            return job

    def _update(self, job: SnapshotJob, *, phase: str, progress: int, status: str | None = None) -> None:
        with self._jobs_lock:
            job.phase = phase
            job.progress = max(0, min(progress, 100))
            job.status = status or job.status
            job.updated_at = time.time()

    def _finish_failed_job(self, job: SnapshotJob, *, phase: str, status: str, error: str) -> None:
        """Publish a terminal job state even when local metadata is unavailable.

        Sync metadata is deliberately best-effort during error handling.  A
        locked/corrupt ``user.db`` or a repository outage must not leave the
        in-memory job stuck in ``running`` forever, because the UI uses that
        state to decide whether another operation can be started.
        """

        job.error = error[:1000]
        try:
            self.settings.set_sync_status(job.user_id, status, error=error)
        except Exception:
            pass
        self._update(job, phase=phase, progress=100, status=status)

    def _fail_remote_snapshot(self, user_id: str, snapshot_id: str) -> None:
        """Best-effort cleanup of an upload row after a local failure."""

        try:
            self.repository.fail_snapshot(user_id, snapshot_id)
        except Exception:
            # The cloud row is already non-complete from the client's point of
            # view.  A later maintenance pass may remove an abandoned upload.
            pass

    def job(self, user_id: str, job_id: str) -> dict[str, object] | None:
        with self._jobs_lock:
            job = self._jobs.get(job_id)
            return job.public() if job is not None and job.user_id == user_id else None

    def active_job(self, user_id: str) -> dict[str, object] | None:
        with self._jobs_lock:
            jobs = [job for job in self._jobs.values() if job.user_id == user_id]
            if not jobs:
                return None
            return max(jobs, key=lambda item: item.updated_at).public()

    def start_save(self, user_id: str, *, force: bool = False) -> dict[str, object]:
        self._require_allowed(user_id)
        job = self._new_job(user_id, "save")
        if job.status == "queued" and job.phase == "queued":
            self._executor.submit(self._save, job, force)
        return job.public()

    def start_restore(self, user_id: str, snapshot_id: str) -> dict[str, object]:
        self._require_allowed(user_id)
        job = self._new_job(user_id, "restore")
        if job.kind != "restore":
            return job.public()
        job.snapshot_id = snapshot_id
        if job.status == "queued" and job.phase == "queued":
            self._executor.submit(self._restore, job, snapshot_id)
        return job.public()

    def snapshots(self, user_id: str) -> list[dict[str, object]]:
        self._require_allowed(user_id)
        return self.repository.list_snapshots(user_id)

    def mark_dirty(self, user_id: str) -> int:
        self._require_allowed(user_id)
        return self.settings.mark_dirty(user_id)

    def notify_run_finished(self, user_id: str) -> None:
        if not self._user_allowed(user_id):
            return
        self.mark_dirty(user_id)
        preferences = self.settings.sync_preferences_for_user(user_id)
        if preferences["auto_save_enabled"] and preferences["auto_save_rule"] == "after_run":
            self.start_save(user_id)

    def _schedule_loop(self) -> None:
        while not self._stop.wait(30):
            now = time.time()
            for root in self.data_root.iterdir():
                if (
                    not root.is_dir()
                    or root.is_symlink()
                    or root.name.startswith(".")
                    or not (root / "user.db").exists()
                ):
                    continue
                user_id = root.name
                try:
                    if not self._user_allowed(user_id):
                        continue
                    preferences = self.settings.sync_preferences_for_user(user_id)
                    state = self.settings.sync_state_for_user(user_id)
                    if not preferences["auto_save_enabled"] or state["status"] not in {"dirty", "local_only"}:
                        continue
                    rule = preferences["auto_save_rule"]
                    updated_at = float(state["updated_at"] or 0)
                    should_save = rule == "idle_5m" and updated_at <= now - 300
                    if rule == "hourly":
                        should_save = self._last_auto_attempt.get(user_id, 0) <= now - 3600
                    if should_save:
                        self._last_auto_attempt[user_id] = now
                        self.start_save(user_id)
                except Exception:
                    continue

    def _require_allowed(self, user_id: str) -> None:
        validate_identity_id(user_id, require_uuid=True)
        if not self._user_allowed(user_id):
            raise PermissionError("Guest identities cannot use cloud synchronization.")

    def recover_key_if_available(self, user_id: str) -> bool:
        if self.key_store.get(user_id) is not None:
            return True
        recovered = self.repository.recover_user_key(user_id)
        if recovered is None:
            return False
        self.key_store.set(user_id, recovered)
        return True

    def _user_paths(self, user_id: str) -> ClientPaths:
        if not user_id or Path(user_id).name != user_id or user_id in {".", ".."}:
            raise ValueError("Unsafe user id for snapshot path.")
        validate_identity_id(user_id, require_uuid=True)
        if self.data_root.is_symlink():
            raise ValueError("Snapshot data root cannot be a symbolic link.")
        candidate = self.data_root / user_id
        if candidate.is_symlink():
            raise ValueError("Snapshot user path cannot be a symbolic link.")
        root = candidate.resolve()
        if root.parent != self.data_root:
            raise ValueError("Snapshot path must remain inside the data root.")
        paths = ClientPaths(root)
        paths.ensure()
        return paths

    def _user_key(self, user_id: str) -> bytes:
        local = self.key_store.get(user_id)
        remote = self.repository.recover_user_key(user_id)
        if local is None and remote is not None:
            self.key_store.set(user_id, remote)
            return remote
        if local is not None and remote is not None and local != remote:
            raise CloudSyncConflict("The local user data key does not match the cloud key.")
        if local is None:
            local = self.key_store.get_or_create(user_id)
        self.repository.ensure_user_key(user_id, local)
        return local

    def _save(self, job: SnapshotJob, force: bool) -> None:
        snapshot_id = f"snapshot_{uuid4().hex}"
        job.snapshot_id = snapshot_id
        paths: ClientPaths | None = None
        staging: Path | None = None
        archive: Path | None = None
        begun = False
        with self._lock_for(job.user_id):
            try:
                paths = self._user_paths(job.user_id)
                staging = paths.sync_staging_dir / f"save-{job.id}"
                archive = paths.sync_staging_dir / f"save-{job.id}.zip"
                self._update(job, phase="snapshot", progress=5, status="running")
                self.settings.set_sync_status(job.user_id, "saving")
                state = self.settings.sync_state_for_user(job.user_id)
                local_revision = int(state.get("local_revision") or 0)
                parent = state.get("cloud_snapshot_id")
                key = self._user_key(job.user_id)
                staging.mkdir(parents=True, exist_ok=False)
                self._materialize(paths, staging, job.user_id, local_revision)

                self._update(job, phase="compress", progress=30)
                self._zip(staging, archive)
                archive_hash = self._sha256(archive)
                archive_size = archive.stat().st_size

                self._update(job, phase="encrypt", progress=45)
                self.repository.begin_snapshot(
                    snapshot_id=snapshot_id,
                    user_id=job.user_id,
                    parent_snapshot_id=str(parent) if parent else None,
                    local_revision=local_revision,
                    device_id=self._device_id_for(job.user_id),
                    force=force,
                )
                begun = True
                count = self._upload_chunks(job, archive, key, snapshot_id)
                self.repository.complete_snapshot(
                    job.user_id,
                    snapshot_id,
                    archive_sha256=archive_hash,
                    archive_size=archive_size,
                    chunk_count=count,
                )
                self.settings.mark_uploaded(job.user_id, snapshot_id, local_revision)
                self._update(job, phase="complete", progress=100, status="complete")
            except CloudSyncConflict as exc:
                if begun:
                    self._fail_remote_snapshot(job.user_id, snapshot_id)
                self._finish_failed_job(job, phase="conflict", status="conflict", error=str(exc))
            except Exception as exc:
                if begun:
                    self._fail_remote_snapshot(job.user_id, snapshot_id)
                safe_error = f"{type(exc).__name__}: {exc}"
                self._finish_failed_job(job, phase="failed", status="failed", error=safe_error)
            finally:
                if staging is not None:
                    shutil.rmtree(staging, ignore_errors=True)
                if archive is not None:
                    archive.unlink(missing_ok=True)

    def _materialize(self, paths: ClientPaths, staging: Path, user_id: str, revision: int) -> None:
        staging.mkdir(parents=True, exist_ok=True)
        if paths.config_file.is_symlink() or (paths.config_file.exists() and not paths.config_file.is_file()):
            raise ValueError("Snapshot config.toml must be a regular file.")
        if paths.config_file.exists():
            shutil.copy2(paths.config_file, staging / "config.toml")
        if paths.user_db.is_symlink() or (paths.user_db.exists() and not paths.user_db.is_file()):
            raise ValueError("Snapshot user.db must be a regular file.")
        if paths.user_db.exists():
            self._backup_sqlite(paths.user_db, staging / "user.db")
        for name, source in (
            ("skills", paths.skills_dir),
            ("rag", paths.rag_dir),
            ("plugins", paths.plugins_dir),
            ("mcp", paths.mcp_dir),
        ):
            if source.exists():
                self._copy_tree(
                    source,
                    staging / name,
                    exclude_rag_cache=name == "rag",
                    reject_symlinks=True,
                )
        runtime_target = staging / "runtime"
        for session in paths.runtime_dir.iterdir():
            if session.is_symlink() or not session.is_dir():
                raise ValueError(f"Runtime session is not a regular directory: {session.name}")
            paths.session_root(session.name)
            target = runtime_target / session.name
            state_db = session / "state.db"
            if state_db.is_symlink() or not state_db.is_file():
                raise ValueError(f"Runtime session has no regular state.db: {session.name}")
            if self._session_is_local_only(state_db):
                continue
            target_state_db = target / "state.db"
            self._backup_sqlite(state_db, target_state_db)
            # Uploads live below the workspace in the canonical layout.  A
            # single workspace copy carries them; legacy sibling uploads
            # directories are migrated first so no old upload is dropped.
            paths.migrate_legacy_uploads(session.name)
            source = session / "workspace"
            if source.exists():
                self._copy_tree(
                    source,
                    target / "workspace",
                    exclude_runtime_logs=True,
                    reject_symlinks=True,
                )
            self._index_session_files(
                target_state_db,
                {"workspace": target / "workspace"},
            )
        manifest = {
            "format_version": SNAPSHOT_FORMAT_VERSION,
            "user_id": user_id,
            "local_revision": revision,
            "created_at": time.time(),
            "files": self._manifest_files(staging),
        }
        (staging / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
        )

    @staticmethod
    def _session_is_local_only(database: Path) -> bool:
        connection = sqlite3.connect(database)
        try:
            columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(session_meta)")}
            if "local_only" not in columns:
                return False
            row = connection.execute("SELECT local_only FROM session_meta LIMIT 1").fetchone()
            return bool(row and int(row[0]))
        finally:
            connection.close()

    @staticmethod
    def _copy_tree(
        source: Path,
        target: Path,
        *,
        exclude_rag_cache: bool = False,
        exclude_runtime_logs: bool = False,
        reject_symlinks: bool = False,
    ) -> None:
        # All snapshot-owned copies use the same manifest-compatible
        # exclusion policy. Keep the historical keyword parameters for
        # callers, but do not let one component accidentally include files
        # that restore validation would reject.
        del exclude_rag_cache, exclude_runtime_logs
        if source.is_symlink():
            if reject_symlinks:
                raise ValueError(f"Snapshot source contains a symbolic link: {source}")
            return
        if not source.is_dir():
            raise ValueError(f"Snapshot source is not a directory: {source}")
        if target.is_symlink() or (target.exists() and not target.is_dir()):
            raise ValueError(f"Snapshot target is not a regular directory: {target}")
        target.mkdir(parents=True, exist_ok=True)
        for item in source.rglob("*"):
            relative = item.relative_to(source)
            if item.is_symlink():
                if reject_symlinks:
                    raise ValueError(f"Snapshot source contains a symbolic link: {item}")
                continue
            parts = {part.lower() for part in relative.parts}
            if parts & {
                "cache",
                ".cache",
                "tmp",
                ".tmp",
                "__pycache__",
                ".pytest_cache",
                ".mypy_cache",
                ".ruff_cache",
                ".vite",
                "node_modules",
                "logs",
                "log",
            } or any(part.endswith(_SNAPSHOT_EXCLUDED_SUFFIXES) for part in parts):
                continue
            destination = target / relative
            if item.is_dir():
                destination.mkdir(parents=True, exist_ok=True)
            elif item.is_file():
                destination.parent.mkdir(parents=True, exist_ok=True)
                if reject_symlinks and item.name in {"user.db", "state.db"}:
                    SnapshotManager._backup_sqlite(item, destination)
                else:
                    shutil.copy2(item, destination)
            elif reject_symlinks:
                raise ValueError(f"Snapshot source contains an unsupported file: {item}")

    @staticmethod
    def _backup_sqlite(source: Path, target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        source_connection = sqlite3.connect(source)
        target_connection = sqlite3.connect(target)
        try:
            source_connection.backup(target_connection)
            target_connection.commit()
        finally:
            target_connection.close()
            source_connection.close()

    @classmethod
    def _index_session_files(cls, database: Path, roots: dict[str, Path]) -> None:
        """Record durable workspace/upload hashes inside the session backup."""

        rows: list[tuple[str, int, str, int]] = []
        for prefix, root in roots.items():
            if root.is_symlink():
                raise ValueError(f"Session payload directory cannot be a symbolic link: {root}")
            if not root.exists():
                continue
            if not root.is_dir():
                raise ValueError(f"Session payload path must be a directory: {root}")
            for item in root.rglob("*"):
                if item.is_symlink() or not item.is_file():
                    if item.is_dir() and not item.is_symlink():
                        continue
                    raise ValueError(f"Session payload contains an unsupported file: {item}")
                relative = f"{prefix}/{item.relative_to(root).as_posix()}"
                stat = item.stat()
                rows.append((relative, stat.st_size, cls._sha256(item), stat.st_mtime_ns))

        connection = sqlite3.connect(database)
        try:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS workspace_files (
                session_id TEXT NOT NULL, relative_path TEXT NOT NULL,
                size INTEGER NOT NULL, sha256 TEXT NOT NULL, mtime_ns INTEGER NOT NULL,
                PRIMARY KEY (session_id, relative_path))"""
            )
            connection.execute("DELETE FROM workspace_files")
            session_id = database.parent.name
            connection.executemany(
                "INSERT INTO workspace_files(session_id,relative_path,size,sha256,mtime_ns) VALUES (?,?,?,?,?)",
                [(session_id, relative, size, digest, mtime_ns) for relative, size, digest, mtime_ns in rows],
            )
            connection.commit()
        finally:
            connection.close()

    @classmethod
    def _manifest_files(cls, root: Path) -> list[dict[str, object]]:
        return [
            {
                "path": path.relative_to(root).as_posix(),
                "size": path.stat().st_size,
                "sha256": cls._sha256(path),
            }
            for path in sorted(root.rglob("*"))
            if path.is_file() and path.name != "manifest.json"
        ]

    @staticmethod
    def _zip(source: Path, target: Path) -> None:
        with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for path in sorted(source.rglob("*")):
                if path.is_file():
                    archive.write(path, path.relative_to(source).as_posix())

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(CHUNK_SIZE), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _upload_chunks(self, job: SnapshotJob, archive: Path, key: bytes, snapshot_id: str) -> int:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        cipher = AESGCM(key)
        total = max(archive.stat().st_size, 1)
        uploaded = 0
        sequence = 0
        with archive.open("rb") as handle:
            while plaintext := handle.read(CHUNK_SIZE):
                nonce = secrets.token_bytes(12)
                aad = f"mini-agent-snapshot:v2:{job.user_id}:{snapshot_id}:{sequence}".encode()
                ciphertext = cipher.encrypt(nonce, plaintext, aad)
                chunk = EncryptedSnapshotChunk(
                    sequence,
                    nonce,
                    ciphertext,
                    hashlib.sha256(ciphertext).hexdigest(),
                )
                append_for_user = getattr(self.repository, "append_chunk_for_user", None)
                if callable(append_for_user):
                    append_for_user(job.user_id, snapshot_id, chunk)
                else:
                    self.repository.append_chunk(snapshot_id, chunk)
                uploaded += len(plaintext)
                sequence += 1
                self._update(job, phase="upload", progress=50 + int(uploaded / total * 45))
        return sequence

    def _restore(self, job: SnapshotJob, snapshot_id: str) -> None:
        paths: ClientPaths | None = None
        restore_base: Path | None = None
        archive: Path | None = None
        extracted: Path | None = None
        recovery: Path | None = None
        with self._lock_for(job.user_id):
            try:
                paths = self._user_paths(job.user_id)
                restore_base = paths.sync_staging_dir / f"restore-{job.id}"
                archive = restore_base / "snapshot.zip"
                extracted = restore_base / "payload"
                recovery = paths.sync_recovery_dir / f"restore-{int(time.time())}-{job.id}"
                if self._has_active_run(paths):
                    raise RuntimeError("存在正在运行的 Agent 任务，请停止后再恢复云端版本。")
                # Capture the complete local whitelist before any remote
                # bytes are downloaded or decrypted. A bad key, checksum, or
                # manifest must still leave a usable recovery copy.
                recovery.mkdir(parents=True, exist_ok=False)
                self._copy_owned_components(paths.root, recovery, include_local_runtime=True)
                self._update(job, phase="download", progress=5, status="running")
                self.settings.set_sync_status(job.user_id, "restoring")
                metadata, chunks = self.repository.download(job.user_id, snapshot_id)
                key = self._user_key(job.user_id)
                restore_base.mkdir(parents=True, exist_ok=False)
                try:
                    expected_archive_size = int(metadata["archive_size"])
                    expected_chunk_count = int(metadata["chunk_count"])
                except (KeyError, TypeError, ValueError) as exc:
                    raise ValueError("Cloud snapshot metadata is invalid.") from exc
                if expected_archive_size < 1 or expected_chunk_count < 1:
                    raise ValueError("Cloud snapshot metadata is invalid.")
                self._download_chunks(
                    job,
                    archive,
                    key,
                    snapshot_id,
                    chunks,
                    expected_archive_size=expected_archive_size,
                    expected_chunk_count=expected_chunk_count,
                )
                if self._sha256(archive) != metadata["archive_sha256"]:
                    raise ValueError("Cloud archive checksum mismatch.")
                self._update(job, phase="verify", progress=65)
                self._safe_extract(archive, extracted)
                self._verify_manifest(extracted, job.user_id)

                self._update(job, phase="replace", progress=85)
                assert recovery is not None and paths is not None
                try:
                    self._replace_payload(extracted, paths.root)
                    # Project sessions are deliberately absent from the cloud
                    # archive. Restore them from the local recovery copy after
                    # replacing the sync-owned runtime payload.
                    self._restore_local_runtime_sessions(recovery / "runtime", paths.runtime_dir)
                except Exception as replace_error:
                    try:
                        self._replace_payload(recovery, paths.root)
                    except Exception as rollback_error:
                        raise RuntimeError(
                            "云端快照替换失败且本地回滚也失败，请保留 recovery 副本人工恢复。"
                        ) from rollback_error
                    raise replace_error
                restored_paths = ClientPaths(paths.root)
                restored_paths.ensure()
                # ZIP archives do not retain empty directories.  Recreate
                # the per-session workspace/upload contract after the
                # allowlisted payload has been validated and replaced.
                for session in restored_paths.runtime_dir.iterdir():
                    if session.is_symlink() or not session.is_dir():
                        raise ValueError("Restored runtime contains an invalid session directory.")
                    restored_paths.ensure_session(session.name)
                invalidate = getattr(self.settings, "invalidate", None)
                if callable(invalidate):
                    invalidate(job.user_id)
                self.settings.mark_uploaded(
                    job.user_id,
                    snapshot_id,
                    int(metadata["local_revision"]),
                )
                self._prune_recovery(paths.sync_recovery_dir, keep=recovery.name)
                self._update(job, phase="complete", progress=100, status="complete")
            except Exception as exc:
                safe_error = f"{type(exc).__name__}: {exc}"
                job.error = safe_error[:1000]
                try:
                    self.settings.set_sync_status(job.user_id, "error", error=safe_error)
                except Exception:
                    pass
                self._update(job, phase="failed", progress=100, status="failed")
            finally:
                if restore_base is not None:
                    shutil.rmtree(restore_base, ignore_errors=True)

    @staticmethod
    def _replace_payload(source: Path, target: Path) -> None:
        """Replace snapshot-owned components while preserving local sync metadata."""

        if source.is_symlink() or not source.is_dir():
            raise ValueError("Snapshot replacement source must be a regular directory.")
        if target.is_symlink() or not target.is_dir():
            raise ValueError("Snapshot replacement target must be a regular directory.")
        components = ("config.toml", "user.db", "skills", "rag", "plugins", "mcp", "runtime")
        # Validate every destination before deleting any component.  This
        # keeps a malicious local symlink from turning a failed restore into a
        # partially replaced user tree.
        for name in components:
            target_item = target / name
            if target_item.is_symlink():
                raise ValueError(f"Snapshot replacement target contains a symbolic link: {target_item}")
            if target_item.exists() and not (target_item.is_file() or target_item.is_dir()):
                raise ValueError(f"Snapshot replacement target contains a special file: {target_item}")
            source_item = source / name
            if source_item.is_symlink():
                raise ValueError(f"Snapshot replacement source contains a symbolic link: {source_item}")
            if source_item.exists() and not (source_item.is_file() or source_item.is_dir()):
                raise ValueError(f"Snapshot replacement source contains a special file: {source_item}")
        target.mkdir(parents=True, exist_ok=True)
        for name in components:
            source_item = source / name
            target_item = target / name
            if target_item.is_dir():
                shutil.rmtree(target_item)
            else:
                target_item.unlink(missing_ok=True)
            if not source_item.exists():
                continue
            if source_item.is_dir():
                shutil.copytree(source_item, target_item)
            else:
                shutil.copy2(source_item, target_item)

    @classmethod
    def _restore_local_runtime_sessions(cls, source: Path, target: Path) -> None:
        if not source.exists():
            return
        if source.is_symlink() or not source.is_dir():
            raise ValueError("Local recovery runtime is not a regular directory.")
        target.mkdir(parents=True, exist_ok=True)
        for session in source.iterdir():
            if session.is_symlink() or not session.is_dir():
                raise ValueError(f"Local recovery runtime contains an invalid session: {session.name}")
            state_db = session / "state.db"
            if not state_db.is_file() or state_db.is_symlink() or not cls._session_is_local_only(state_db):
                continue
            destination = target / session.name
            if destination.exists():
                if destination.is_symlink() or not destination.is_dir():
                    raise ValueError(f"Runtime restore target is invalid: {destination}")
                shutil.rmtree(destination)
            # Local project sessions are outside the cloud snapshot contract.
            # Their recovery copy must therefore not apply the snapshot's
            # cache/log/node_modules filters: uploads are user data and must
            # survive a cloud restore byte-for-byte (subject to the same
            # symlink/special-file safety checks).
            cls._copy_exact_tree(session, destination)

    @staticmethod
    def _copy_exact_tree(source: Path, target: Path) -> None:
        """Copy a local recovery tree without snapshot content filtering."""

        if source.is_symlink() or not source.is_dir():
            raise ValueError(f"Recovery source is not a regular directory: {source}")
        if target.is_symlink() or (target.exists() and not target.is_dir()):
            raise ValueError(f"Recovery target is not a regular directory: {target}")
        target.mkdir(parents=True, exist_ok=True)
        for item in source.rglob("*"):
            relative = item.relative_to(source)
            destination = target / relative
            if item.is_symlink() or destination.is_symlink():
                raise ValueError(f"Recovery tree contains a symbolic link: {item}")
            if item.is_dir():
                destination.mkdir(parents=True, exist_ok=True)
            elif item.is_file():
                destination.parent.mkdir(parents=True, exist_ok=True)
                if item.name in {"user.db", "state.db"}:
                    SnapshotManager._backup_sqlite(item, destination)
                else:
                    shutil.copy2(item, destination)
            else:
                raise ValueError(f"Recovery tree contains an unsupported file: {item}")

    @staticmethod
    def _copy_owned_components(source: Path, target: Path, *, include_local_runtime: bool = False) -> None:
        for name in ("config.toml", "user.db", "skills", "rag", "plugins", "mcp", "runtime"):
            item = source / name
            if name == "runtime" and not include_local_runtime:
                # Callers that only need sync-owned recovery can omit runtime;
                # restore recovery opts in to preserve local-only sessions.
                continue
            if item.is_symlink():
                raise ValueError(f"Snapshot source contains a symbolic link: {item}")
            if not item.exists():
                continue
            destination = target / name
            if item.is_dir():
                if name == "runtime" and include_local_runtime:
                    # Recovery is local-only and must retain every file in a
                    # project session, including names that the cloud
                    # snapshot filter intentionally excludes.
                    SnapshotManager._copy_exact_tree(item, destination)
                else:
                    SnapshotManager._copy_tree(
                        item,
                        destination,
                        exclude_rag_cache=name == "rag",
                        reject_symlinks=True,
                    )
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                if item.name in {"user.db", "state.db"}:
                    SnapshotManager._backup_sqlite(item, destination)
                else:
                    shutil.copy2(item, destination)

    @staticmethod
    def _prune_recovery(directory: Path, *, keep: str) -> None:
        candidates: list[Path] = []
        for item in directory.iterdir():
            if item.is_symlink():
                raise ValueError(f"Recovery directory contains a symbolic link: {item}")
            if item.is_dir() and item.name.startswith("restore-"):
                candidates.append(item)
        candidates.sort(key=lambda item: item.stat().st_mtime, reverse=True)
        for item in candidates:
            if item.name != keep:
                shutil.rmtree(item, ignore_errors=True)

    @staticmethod
    def _has_active_run(paths: ClientPaths) -> bool:
        for session in paths.runtime_dir.iterdir():
            if not session.is_dir() or session.is_symlink():
                continue
            database = session / "state.db"
            if database.is_symlink() or not database.is_file():
                continue
            connection = None
            try:
                connection = sqlite3.connect(database)
                if connection.execute("SELECT 1 FROM session_runs WHERE status='running' LIMIT 1").fetchone():
                    return True
            except sqlite3.Error:
                continue
            finally:
                if connection is not None:
                    connection.close()
        return False

    def _download_chunks(
        self,
        job: SnapshotJob,
        archive: Path,
        key: bytes,
        snapshot_id: str,
        chunks: list[EncryptedSnapshotChunk],
        *,
        expected_archive_size: int,
        expected_chunk_count: int,
    ) -> None:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        cipher = AESGCM(key)
        archive.parent.mkdir(parents=True, exist_ok=True)
        if len(chunks) != expected_chunk_count:
            raise ValueError("Cloud snapshot chunk count mismatch.")
        written = 0
        with archive.open("wb") as handle:
            for index, chunk in enumerate(chunks):
                if chunk.sequence != index or hashlib.sha256(chunk.ciphertext).hexdigest() != chunk.checksum:
                    raise ValueError("Cloud snapshot chunk validation failed.")
                aad = f"mini-agent-snapshot:v2:{job.user_id}:{snapshot_id}:{index}".encode()
                plaintext = cipher.decrypt(chunk.nonce, chunk.ciphertext, aad)
                written += len(plaintext)
                if written > expected_archive_size:
                    raise ValueError("Cloud snapshot archive is larger than declared.")
                handle.write(plaintext)
                self._update(job, phase="download", progress=5 + int((index + 1) / max(len(chunks), 1) * 50))
        if written != expected_archive_size:
            raise ValueError("Cloud snapshot archive size mismatch.")

    @staticmethod
    def _safe_extract(archive_path: Path, target: Path) -> None:
        target.mkdir(parents=True, exist_ok=True)
        root = target.resolve()
        seen: set[str] = set()
        with zipfile.ZipFile(archive_path) as archive:
            for item in archive.infolist():
                name = item.filename.replace("\\", "/")
                normalized = name.rstrip("/")
                canonical = normalized.casefold()
                if (
                    not normalized
                    or "\x00" in name
                    or canonical in {value.casefold() for value in seen}
                    or any(
                        canonical.startswith(f"{value.casefold()}/") or value.casefold().startswith(f"{canonical}/")
                        for value in seen
                    )
                ):
                    raise ValueError("Cloud archive contains duplicate or invalid paths.")
                seen.add(normalized)
                relative = PurePosixPath(name)
                windows_relative = PureWindowsPath(name)
                if (
                    relative.is_absolute()
                    or windows_relative.is_absolute()
                    or windows_relative.drive
                    or ".." in relative.parts
                    or any(part in {"", "."} for part in relative.parts)
                ):
                    raise ValueError("Cloud archive contains an unsafe path.")
                mode = (item.external_attr >> 16) & 0o170000
                if mode == 0o120000:
                    raise ValueError("Cloud archive contains a symbolic link.")
                destination = (root / Path(*relative.parts)).resolve()
                if destination != root and root not in destination.parents:
                    raise ValueError("Cloud archive contains an unsafe path.")
            archive.extractall(root)

    @classmethod
    def _verify_manifest(cls, root: Path, user_id: str) -> None:
        manifest_path = root / "manifest.json"
        try:
            value = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("Cloud snapshot manifest is missing or invalid.") from exc
        if value.get("format_version") != SNAPSHOT_FORMAT_VERSION or value.get("user_id") != user_id:
            raise ValueError("Cloud snapshot manifest is incompatible.")
        files = value.get("files")
        if not isinstance(files, list):
            raise ValueError("Cloud snapshot manifest has no file list.")
        expected: set[str] = set()
        for item in files:
            if not isinstance(item, dict) or not isinstance(item.get("path"), str):
                raise ValueError("Cloud snapshot manifest contains an invalid entry.")
            relative = str(item["path"]).replace("\\", "/")
            parsed = PurePosixPath(relative)
            windows_relative = PureWindowsPath(relative)
            canonical_relative = relative.casefold()
            if (
                not relative
                or parsed.is_absolute()
                or windows_relative.is_absolute()
                or windows_relative.drive
                or ".." in parsed.parts
                or any(part in {"", "."} for part in parsed.parts)
                or relative == "manifest.json"
                or canonical_relative in {entry.casefold() for entry in expected}
                or any(
                    canonical_relative.startswith(f"{entry.casefold()}/")
                    or entry.casefold().startswith(f"{canonical_relative}/")
                    for entry in expected
                )
                or len(str(item.get("sha256") or "")) != 64
                or any(character not in "0123456789abcdefABCDEF" for character in str(item.get("sha256") or ""))
                or not cls._manifest_path_allowed(parsed.parts)
            ):
                raise ValueError("Cloud snapshot manifest contains an unsafe or duplicate entry.")
            expected.add(relative)
            path = (root / Path(*parsed.parts)).resolve()
            if root.resolve() not in path.parents or path.is_symlink() or not path.is_file():
                raise ValueError("Cloud snapshot file is missing or unsafe.")
            try:
                expected_size = int(item.get("size", -1))
            except (TypeError, ValueError) as exc:
                raise ValueError("Cloud snapshot manifest contains an invalid size.") from exc
            if expected_size < 0 or path.stat().st_size != expected_size or cls._sha256(path) != item.get("sha256"):
                raise ValueError("Cloud snapshot file validation failed.")
        if {item.casefold() for item in expected} < {"config.toml", "user.db"}:
            raise ValueError("Cloud snapshot manifest is missing required user files.")
        actual: set[str] = set()
        expected_casefold = {item.casefold() for item in expected}
        for path in root.rglob("*"):
            if path.is_symlink():
                raise ValueError("Cloud snapshot contains a symbolic link.")
            relative_path = path.relative_to(root).as_posix()
            if path.is_file():
                if path.name == "manifest.json":
                    continue
                actual.add(relative_path)
                continue
            if path.is_dir() and not any(
                entry.startswith(f"{relative_path.casefold()}/") for entry in expected_casefold
            ):
                raise ValueError("Cloud snapshot contains an unexpected directory.")
        if {item.casefold() for item in actual} != expected_casefold:
            raise ValueError("Cloud snapshot contains files outside its manifest.")

    @staticmethod
    def _manifest_path_allowed(parts: tuple[str, ...]) -> bool:
        """Return whether a manifest file belongs to the v2 backup allowlist."""

        if not parts or any(not part or part in {".", ".."} for part in parts):
            return False
        lowered = tuple(part.casefold() for part in parts)
        if any(part in _SNAPSHOT_EXCLUDED_PARTS or part.endswith(_SNAPSHOT_EXCLUDED_SUFFIXES) for part in lowered):
            return False
        if parts in (("config.toml",), ("user.db",)):
            return True
        if lowered[0] in _SNAPSHOT_ROOTS:
            return len(parts) >= 2
        if lowered[0] != "runtime" or len(parts) < 3 or not _SESSION_ID_RE.fullmatch(parts[1]):
            return False
        if parts[2] == "state.db":
            return len(parts) == 3
        return parts[2] in {"workspace", "uploads"} and len(parts) >= 4

    def close(self) -> None:
        self._stop.set()
        self._scheduler.join(timeout=2)
        self._executor.shutdown(wait=False, cancel_futures=True)
