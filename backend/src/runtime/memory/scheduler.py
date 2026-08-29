"""Persistent Memory scheduling, leases, retries, and process lifecycle."""

from __future__ import annotations

import hashlib
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from backend.configuration import ConfigurationError, validate_identity_id
from backend.domain.memory import MemoryJob, MemoryJobKind, MemoryJobStatus, MemorySettings
from backend.jobs import AdmissionPolicy, JobLane, JobScopeKind, QueueMode, ThreadJob
from backend.storage.memory import MemoryConflictError, MemoryNotFoundError, MemoryStore

from .consolidation import ManualMemoryConsolidator
from .extraction import ManualEpisodicExtractor
from .provider_models import MemoryModelUnavailable

logger = logging.getLogger(__name__)


class MemoryJobStore(Protocol):
    def enqueue_job(self, job: MemoryJob) -> MemoryJob: ...

    def enqueue_job_if_absent(self, job: MemoryJob) -> tuple[MemoryJob, bool]: ...

    def claim_job(
        self,
        worker_id: str,
        *,
        kind: MemoryJobKind | None = None,
        now: str | None = None,
        lease_seconds: int = 3600,
    ) -> MemoryJob | None: ...

    def complete_job(self, job_id: str, worker_id: str, *, completed_at: str | None = None) -> MemoryJob: ...

    def fail_job(
        self,
        job_id: str,
        worker_id: str,
        error: str,
        *,
        retry_at: str | None = None,
        failed_at: str | None = None,
    ) -> MemoryJob: ...

    def cancel_job(self, job_id: str, *, cancelled_at: str | None = None, reason: str = "") -> MemoryJob: ...


class MemoryJobScheduler:
    """Small explicit API over persisted lease and retry state."""

    def __init__(self, store: MemoryJobStore) -> None:
        self._store = store

    def enqueue(self, job: MemoryJob) -> MemoryJob:
        return self._store.enqueue_job(job)

    def enqueue_if_absent(self, job: MemoryJob) -> tuple[MemoryJob, bool]:
        return self._store.enqueue_job_if_absent(job)

    def claim(
        self,
        worker_id: str,
        *,
        kind: MemoryJobKind | None = None,
        now: str | None = None,
        lease_seconds: int = 3600,
    ) -> MemoryJob | None:
        return self._store.claim_job(worker_id, kind=kind, now=now, lease_seconds=lease_seconds)

    def complete(self, job_id: str, worker_id: str, *, completed_at: str | None = None) -> MemoryJob:
        return self._store.complete_job(job_id, worker_id, completed_at=completed_at)

    def fail(
        self,
        job_id: str,
        worker_id: str,
        error: str,
        *,
        retry_at: str | None = None,
        failed_at: str | None = None,
    ) -> MemoryJob:
        return self._store.fail_job(job_id, worker_id, error, retry_at=retry_at, failed_at=failed_at)

    def cancel(self, job_id: str, *, reason: str = "") -> MemoryJob:
        return self._store.cancel_job(job_id, reason=reason)


@dataclass(frozen=True)
class MemoryAutomationSettings:
    idle_seconds: int = 300
    scan_interval_seconds: float = 30.0
    phase1_concurrency: int = 2
    lease_seconds: int = 900
    retry_base_seconds: int = 30
    retry_max_seconds: int = 1800

    def __post_init__(self) -> None:
        if self.idle_seconds < 0:
            raise ValueError("idle_seconds must not be negative.")
        if self.scan_interval_seconds <= 0:
            raise ValueError("scan_interval_seconds must be positive.")
        if self.phase1_concurrency < 1:
            raise ValueError("phase1_concurrency must be positive.")
        if self.lease_seconds < 1 or self.retry_base_seconds < 1 or self.retry_max_seconds < self.retry_base_seconds:
            raise ValueError("Memory lease and retry settings are invalid.")


class MemoryAutomationService:
    """Scan idle sessions and execute durable Phase-1/Phase-2 jobs safely."""

    def __init__(
        self,
        state: Any,
        model_factory: Callable[[str], object],
        *,
        settings: MemoryAutomationSettings | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._state = state
        self._model_factory = model_factory
        self.settings = settings or MemoryAutomationSettings()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._lock = threading.RLock()
        self._user_locks: dict[str, threading.RLock] = {}
        self._active: dict[str, tuple[str, ThreadJob]] = {}
        self._phase2_active = False
        self._service_job: ThreadJob | None = None
        self._wake_event = threading.Event()
        self._worker_id = f"memory_worker_{hashlib.sha256(str(id(self)).encode()).hexdigest()[:16]}"

    def start(self) -> None:
        """Start one process-owned scanner in the SERVICE lane."""

        with self._lock:
            if self._service_job is not None:
                return
            job = ThreadJob("memory_automation_service", self._run_loop)
            self._service_job = job
        try:
            self._state.job_registry.submit(
                job,
                scope=self._state.system_job_scope,
                lane=JobLane.SERVICE,
                admission=AdmissionPolicy(queue_mode=QueueMode.REJECT, queue_timeout_seconds=0),
            )
        except Exception:
            with self._lock:
                self._service_job = None
            raise

    def close(self) -> None:
        job = self._service_job
        if job is not None:
            job.cancel("memory automation closed")
        self._wake_event.set()

    def wake(self) -> None:
        """Request an early scan after a setting change or manual enqueue."""

        self._wake_event.set()

    def enqueue_extract(self, user_id: str, session_id: str, *, project_id: str | None = None) -> MemoryJob:
        return self._enqueue_extract(user_id, session_id, project_id=project_id, wake=True)

    def _enqueue_extract(
        self,
        user_id: str,
        session_id: str,
        *,
        project_id: str | None,
        wake: bool,
    ) -> MemoryJob:
        if not self._memory_settings(user_id).generate_memories:
            raise ValueError("Memory generation is disabled.")
        job = MemoryJob.new(kind=MemoryJobKind.EXTRACT, source_id=session_id, project_id=project_id)
        stored, _created = MemoryStore(self._state.user_paths(user_id)).enqueue_job_if_absent(job)
        if wake:
            self.wake()
        return stored

    def enqueue_consolidate(self, user_id: str, *, project_id: str | None = None) -> MemoryJob:
        if not self._memory_settings(user_id).generate_memories:
            raise ValueError("Memory generation is disabled.")
        job = MemoryJob.new(
            kind=MemoryJobKind.CONSOLIDATE,
            source_id=f"scope_{project_id or 'global'}",
            project_id=project_id,
        )
        stored, _created = MemoryStore(self._state.user_paths(user_id)).enqueue_job_if_absent(job)
        self.wake()
        return stored

    def cancel(self, user_id: str, job_id: str) -> MemoryJob:
        store = MemoryStore(self._state.user_paths(user_id))
        cancelled = store.cancel_job(job_id, reason="user_cancelled")
        self._cancel_active(user_id, job_id, "user requested cancellation")
        return cancelled

    def stop_user(self, user_id: str) -> None:
        """Cancel all active Memory work after generation is switched off."""

        store = MemoryStore(self._state.user_paths(user_id))
        for job in store.list_jobs(limit=1000):
            if job.status not in {MemoryJobStatus.PENDING, MemoryJobStatus.RUNNING}:
                continue
            self._cancel_safely(store, job.job_id, "generation_disabled")
            self._cancel_active(user_id, job.job_id, "memory generation disabled")

    def clear_user(self, user_id: str) -> None:
        """Cancel the user's work and clear it after in-flight writes stop."""

        store = MemoryStore(self._state.user_paths(user_id))
        for job in store.list_jobs(limit=1000):
            if job.status not in {MemoryJobStatus.PENDING, MemoryJobStatus.RUNNING}:
                continue
            self._cancel_safely(store, job.job_id, "memory_cleared")
            self._cancel_active(user_id, job.job_id, "memory cleared")
        with self._user_lock(user_id):
            store.clear_all()
            store.rebuild_projections()

    def scan_once(self) -> None:
        """Run one non-blocking discovery and dispatch cycle (also used by tests)."""

        users = self._discover_users()
        for user_id in users:
            try:
                self._scan_user(user_id)
            except Exception as exc:
                logger.warning("memory scan skipped for one user: %s", exc.__class__.__name__)
        self._dispatch(users)

    def _run_loop(self, *, is_cancelled: Callable[[], bool]) -> None:
        while not is_cancelled():
            self.scan_once()
            self._wake_event.wait(self.settings.scan_interval_seconds)
            self._wake_event.clear()
            if is_cancelled():
                break

    def _discover_users(self) -> list[str]:
        result: list[str] = []
        root = Path(self._state.data_root)
        if not root.is_dir():
            return result
        for candidate in root.iterdir():
            if not candidate.is_dir() or candidate.is_symlink():
                continue
            try:
                validate_identity_id(candidate.name, require_uuid=True)
            except (ConfigurationError, ValueError):
                continue
            try:
                if self._memory_settings(candidate.name).generate_memories:
                    result.append(candidate.name)
            except Exception:
                continue
        return result

    def _scan_user(self, user_id: str) -> None:
        from backend.api.session_store import session_store

        settings = self._memory_settings(user_id)
        if not settings.generate_memories or not settings.automatic_memory_enabled:
            return
        sessions = session_store(self._state, user_id)
        store = MemoryStore(self._state.user_paths(user_id))
        cutoff = self._clock() - timedelta(seconds=self.settings.idle_seconds)
        for summary in sessions.list_sessions(state="all"):
            if summary.deleted_at is not None or summary.last_run_status == "running" or summary.message_count < 2:
                continue
            try:
                updated = datetime.fromisoformat(summary.updated_at)
            except ValueError:
                continue
            if updated > cutoff:
                continue
            watermark = store.get_watermark(summary.session_id)
            if watermark is not None and watermark.position >= summary.message_count:
                continue
            project = self._state.projects(user_id).session_project(summary.session_id)
            project_id = project.project_id if project is not None and project.removed_at is None else None
            self._enqueue_extract(
                user_id,
                summary.session_id,
                project_id=project_id,
                wake=False,
            )

    def _dispatch(self, users: list[str]) -> None:
        with self._lock:
            extract_capacity = self.settings.phase1_concurrency - len(self._active)
        for user_id in users:
            if extract_capacity <= 0:
                break
            if self._user_has_active(user_id):
                continue
            store = MemoryStore(self._state.user_paths(user_id))
            claimed = store.claim_job(
                self._worker_id,
                kind=MemoryJobKind.EXTRACT,
                lease_seconds=self.settings.lease_seconds,
            )
            if claimed is not None and self._submit(user_id, claimed):
                extract_capacity -= 1

        with self._lock:
            phase2_available = not self._phase2_active
        if not phase2_available:
            return
        for user_id in users:
            store = MemoryStore(self._state.user_paths(user_id))
            claimed = store.claim_job(
                self._worker_id,
                kind=MemoryJobKind.CONSOLIDATE,
                lease_seconds=self.settings.lease_seconds,
            )
            if claimed is not None:
                with self._lock:
                    self._phase2_active = True
                if not self._submit(user_id, claimed):
                    with self._lock:
                        self._phase2_active = False
                break

    def _submit(self, user_id: str, persisted: MemoryJob) -> bool:
        execution_id = f"{persisted.job_id}_attempt_{persisted.attempts}"
        job = ThreadJob(execution_id, self._execute, args=(user_id, persisted))
        with self._lock:
            self._active[persisted.job_id] = (user_id, job)
        scope = self._state.system_job_scope.child(JobScopeKind.USER, user_id=user_id)
        try:
            self._state.job_registry.submit(
                job,
                scope=scope,
                lane=JobLane.BACKGROUND,
                admission=AdmissionPolicy(queue_mode=QueueMode.REJECT, queue_timeout_seconds=0),
            )
        except Exception as exc:
            with self._lock:
                self._active.pop(persisted.job_id, None)
            self._retry(MemoryStore(self._state.user_paths(user_id)), persisted, exc)
            return False
        return True

    def _execute(self, user_id: str, persisted: MemoryJob, *, is_cancelled: Callable[[], bool]) -> None:
        store = MemoryStore(self._state.user_paths(user_id))
        try:
            with self._user_lock(user_id):
                if is_cancelled() or self._is_cancelled(store, persisted.job_id):
                    return
                settings = self._memory_settings(user_id)
                if not settings.generate_memories:
                    self._cancel_safely(store, persisted.job_id, "generation_disabled")
                    return
                model = _CancellableMemoryModel(
                    self._model_factory(user_id),
                    lambda: is_cancelled() or self._is_cancelled(store, persisted.job_id),
                )
                if persisted.kind is MemoryJobKind.EXTRACT:
                    from backend.api.session_store import session_store

                    result = ManualEpisodicExtractor.from_settings(store, model, settings).extract_session(
                        session_store(self._state, user_id),
                        persisted.source_id or "",
                        project_id=persisted.project_id,
                    )
                    if result.model_called and result.records:
                        self.enqueue_consolidate(user_id, project_id=persisted.project_id)
                elif persisted.kind is MemoryJobKind.CONSOLIDATE:
                    ManualMemoryConsolidator.from_settings(store, model, settings).consolidate(
                        project_id=persisted.project_id
                    )
                if not is_cancelled() and not self._is_cancelled(store, persisted.job_id):
                    store.complete_job(persisted.job_id, self._worker_id)
        except _MemoryWorkCancelled:
            current = store.get_job(persisted.job_id)
            if current is not None and current.status is MemoryJobStatus.RUNNING:
                try:
                    store.fail_job(
                        persisted.job_id,
                        self._worker_id,
                        "cancelled_before_write",
                        retry_at=self._clock().isoformat(),
                    )
                except (MemoryConflictError, MemoryNotFoundError):
                    pass
        except MemoryModelUnavailable as exc:
            self._cancel_safely(store, persisted.job_id, str(exc))
        except MemoryConflictError:
            pass
        except Exception as exc:
            self._retry(store, persisted, exc)
        finally:
            with self._lock:
                self._active.pop(persisted.job_id, None)
                if persisted.kind is MemoryJobKind.CONSOLIDATE:
                    self._phase2_active = False

    def _retry(self, store: MemoryStore, job: MemoryJob, exc: Exception) -> None:
        delay = min(
            self.settings.retry_max_seconds,
            self.settings.retry_base_seconds * (2 ** max(job.attempts - 1, 0)),
        )
        retry_at = (self._clock() + timedelta(seconds=delay)).isoformat()
        error = _safe_error_label(exc)
        try:
            store.fail_job(job.job_id, self._worker_id, error, retry_at=retry_at)
        except (MemoryConflictError, MemoryNotFoundError):
            pass
        logger.warning("memory job failed safely: %s", error)

    @staticmethod
    def _is_cancelled(store: MemoryStore, job_id: str) -> bool:
        current = store.get_job(job_id)
        return current is None or current.status is MemoryJobStatus.CANCELLED

    @staticmethod
    def _cancel_safely(store: MemoryStore, job_id: str, reason: str) -> None:
        try:
            store.cancel_job(job_id, reason=reason)
        except (MemoryConflictError, MemoryNotFoundError):
            pass

    def _user_has_active(self, user_id: str) -> bool:
        with self._lock:
            return any(owner == user_id for owner, _job in self._active.values())

    def _cancel_active(self, user_id: str, job_id: str, reason: str) -> bool:
        with self._lock:
            active = self._active.get(job_id)
        if active is None or active[0] != user_id:
            return False
        return active[1].cancel(reason)

    def _user_lock(self, user_id: str) -> threading.RLock:
        with self._lock:
            return self._user_locks.setdefault(user_id, threading.RLock())

    def _memory_settings(self, user_id: str) -> MemorySettings:
        return MemorySettings.from_mapping(self._state.settings.memory_config_for_user(user_id))


class _MemoryWorkCancelled(RuntimeError):
    """Internal cooperative stop before a model result reaches storage."""


class _CancellableMemoryModel:
    def __init__(self, model: object, cancelled: Callable[[], bool]) -> None:
        self._model = model
        self._cancelled = cancelled

    def extract_episodic(self, request):
        self._check()
        result = self._model.extract_episodic(request)  # type: ignore[attr-defined]
        self._check()
        return result

    def consolidate_memories(self, request):
        self._check()
        result = self._model.consolidate_memories(request)  # type: ignore[attr-defined]
        self._check()
        return result

    def _check(self) -> None:
        if self._cancelled():
            raise _MemoryWorkCancelled


def _safe_error_label(exc: Exception) -> str:
    name = getattr(exc, "name", None)
    if isinstance(exc, AttributeError) and isinstance(name, str) and name.isidentifier():
        return f"AttributeError:{name}"
    return exc.__class__.__name__


__all__ = [
    "MemoryAutomationService",
    "MemoryAutomationSettings",
    "MemoryJobScheduler",
    "MemoryJobStore",
]
