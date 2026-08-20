"""Local-first manager for encrypted JSON event push/pull synchronization."""

from __future__ import annotations

import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from uuid import uuid4

from backend.api.user_data import user_paths
from backend.storage.auth.crypto import UserDataKeyStore
from backend.storage.sqlite import SQLiteSessionStore

from .events import decrypt_event_batch, encrypt_event_batch


@dataclass
class EventSyncJob:
    id: str
    user_id: str
    kind: str
    status: str = "queued"
    phase: str = "queued"
    progress: int = 0
    error: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def public(self) -> dict[str, object]:
        value = asdict(self)
        value.pop("user_id", None)
        return value


class EventSyncManager:
    """Synchronize session JSON events without full-directory snapshots."""

    def __init__(self, data_root: Path, settings, repository, *, user_allowed=None, job_registry=None) -> None:
        del job_registry
        self.data_root = Path(data_root).resolve()
        self.settings = settings
        self.repository = repository
        self._user_allowed = user_allowed or (lambda _user_id: True)
        self.key_store = UserDataKeyStore()
        self._jobs: dict[str, EventSyncJob] = {}
        self._lock = threading.Lock()

    def _device_id_for(self, user_id: str) -> str:
        getter = getattr(self.settings, "device_id_for_user", None)
        return str(getter(user_id)) if callable(getter) else f"device_{user_id}"

    def mark_dirty(self, user_id: str) -> None:
        marker = getattr(self.settings, "mark_dirty", None)
        if callable(marker):
            marker(user_id)

    def notify_run_finished(self, user_id: str) -> dict[str, object]:
        return self.start_save(user_id, force=False)

    def active_job(self, user_id: str) -> dict[str, object] | None:
        with self._lock:
            for job in reversed(list(self._jobs.values())):
                if job.user_id == user_id and job.status in {"queued", "running"}:
                    return job.public()
        return None

    def job(self, user_id: str, job_id: str) -> dict[str, object] | None:
        with self._lock:
            job = self._jobs.get(job_id)
            return job.public() if job is not None and job.user_id == user_id else None

    def start_save(self, user_id: str, *, force: bool = False) -> dict[str, object]:
        del force
        if not self._user_allowed(user_id):
            raise PermissionError("This identity cannot use cloud synchronization.")
        active = self.active_job(user_id)
        if active is not None:
            return active
        job = EventSyncJob(f"syncjob_{uuid4().hex}", user_id, "sync")
        with self._lock:
            self._jobs[job.id] = job
        worker = threading.Thread(target=self._run, args=(job,), name="mini-agent-event-sync", daemon=True)
        worker.start()
        return job.public()

    def sync_now(self, user_id: str, *, force: bool = False) -> dict[str, object]:
        """Start one push/pull pass; retained as the public event API name."""

        return self.start_save(user_id, force=force)

    def cancel(self, user_id: str, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.user_id != user_id or job.status not in {"queued", "running"}:
                return False
            job.status = "cancelled"
            job.updated_at = time.time()
            return True

    def snapshots(self, user_id: str) -> list[dict[str, object]]:
        del user_id
        return []

    def start_restore(self, user_id: str, snapshot_id: str) -> dict[str, object]:
        del user_id, snapshot_id
        raise ValueError("全量云端快照已移除，请使用事件同步恢复。")

    def recover_key_if_available(self, user_id: str) -> bytes | None:
        key = self.key_store.get(user_id)
        if key is not None:
            return key
        remote = self.repository.recover_user_key(user_id)
        if remote is not None:
            self.key_store.set(user_id, remote)
        return remote

    def _run(self, job: EventSyncJob) -> None:
        try:
            self._set(job, status="running", phase="sync", progress=5)
            self._sync_user(job.user_id, job)
            self._set(job, status="completed", phase="done", progress=100)
            setter = getattr(self.settings, "set_sync_status", None)
            if callable(setter):
                setter(job.user_id, "synced")
        except Exception as exc:
            self._set(job, status="error", phase="error", error=str(exc)[:1000])
            setter = getattr(self.settings, "set_sync_status", None)
            if callable(setter):
                setter(job.user_id, "error", error=str(exc))

    def _sync_user(self, user_id: str, job: EventSyncJob) -> None:
        key = self.key_store.get_or_create(user_id)
        self.repository.ensure_user_key(user_id, key)
        paths = user_paths(self.data_root, user_id)
        store = SQLiteSessionStore(paths, self._device_id_for(user_id))
        summaries = store.list_sessions(state="all")
        total = max(len(summaries), 1)
        metric = getattr(self.settings, "set_sync_metrics", None)
        for index, summary in enumerate(summaries, start=1):
            if summary.local_only:
                continue
            pending = store.pending_sync_operations()
            if callable(metric):
                session_pending = [item for item in pending if item.get("session_id") == summary.session_id]
                pending_count = sum(len(item.get("events", [])) for item in session_pending)
                metric(
                    user_id,
                    local_revision=store.event_head(summary.session_id),
                    cloud_revision=store.remote_revision(summary.session_id),
                    pending_event_count=pending_count,
                )
            for operation in pending:
                if operation.get("session_id") != summary.session_id:
                    continue
                events = [item for item in operation.get("events", []) if isinstance(item, dict)]
                if not events:
                    continue
                envelope = encrypt_event_batch(events, key, aad=summary.session_id)
                result = self.repository.push_events(
                    user_id,
                    session_id=summary.session_id,
                    parent_revision=int(operation.get("base_revision", 0)),
                    device_id=self._device_id_for(user_id),
                    event_id=str(operation["operation_id"]),
                    envelope=envelope,
                    checksum=str(envelope["checksum"]),
                    event_ids=[str(item["event_id"]) for item in events if item.get("event_id")],
                )
                revision = int(result.get("revision", result.get("head_revision", 0)))
                store.acknowledge_sync_operations(
                    [{"session_id": summary.session_id, "event_ids": [str(item["event_id"]) for item in events], "revision": revision}]
                )
                marker = getattr(self.settings, "mark_uploaded", None)
                if callable(marker):
                    marker(user_id, str(operation["operation_id"]), revision)
            pulled = self.repository.pull_events(
                user_id,
                session_id=summary.session_id,
                after_revision=store.remote_revision(summary.session_id),
            )
            for item in pulled.get("events", []) if isinstance(pulled.get("events"), list) else []:
                if not isinstance(item, dict):
                    continue
                events = decrypt_event_batch(item.get("envelope", {}), key, aad=summary.session_id)
                store.apply_sync_events(
                    {
                        "session_id": summary.session_id,
                        "revision": int(item.get("revision", 0)),
                        "owner_device_id": str(item.get("device_id") or self._device_id_for(user_id)),
                        "events": events,
                    },
                    local_device_id=self._device_id_for(user_id),
                )
            if callable(metric):
                remaining = [item for item in store.pending_sync_operations() if item.get("session_id") == summary.session_id]
                metric(
                    user_id,
                    local_revision=store.event_head(summary.session_id),
                    cloud_revision=store.remote_revision(summary.session_id),
                    pending_event_count=sum(len(op.get("events", [])) for op in remaining),
                )
            self._set(job, progress=min(95, 5 + int(index / total * 90)))

    def _set(self, job: EventSyncJob, **values: object) -> None:
        with self._lock:
            for key, value in values.items():
                setattr(job, key, value)
            job.updated_at = time.time()


__all__ = ["EventSyncJob", "EventSyncManager"]
