"""Public JobRegistry facade and registration/query operations."""

from __future__ import annotations

import threading
from collections import deque
from datetime import UTC, datetime
from typing import Any

from ..base import TERMINAL_STATES, Job, JobState, JobStateListener
from ..errors import JobNotFound, JobQueueFull, JobRegistrationError
from ..scheduling import AdmissionPolicy, JobLane, JobLimitPolicy
from ..scope import JobOwner, JobScope, JobScopeKind
from .admission import _AdmissionRegistryMixin
from .lifecycle import _LifecycleRegistryMixin
from .models import JobQuery, ScopedJobInfo, _Record
from .scopes import _ScopeRegistryMixin


class JobRegistry(_ScopeRegistryMixin, _AdmissionRegistryMixin, _LifecycleRegistryMixin, JobStateListener):
    """Process-wide job control center built on the module 1 state machine."""

    def __init__(
        self,
        *,
        policy: JobLimitPolicy | None = None,
        clock: Any | None = None,
        history_limit: int = 1000,
        session_history_limit: int = 100,
    ) -> None:
        self._policy = policy or JobLimitPolicy.defaults()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._history_limit = history_limit
        self._session_history_limit = session_history_limit
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._id_seq = 0
        self._scope_seq = 0
        self._records: dict[str, _Record] = {}
        self._scopes: dict[str, JobScope] = {}
        self._closed_scopes: set[str] = set()
        self._queues: dict[JobLane, deque[str]] = {lane: deque() for lane in JobLane}
        self._running: dict[JobLane, int] = {lane: 0 for lane in JobLane}
        self._running_by_session: dict[tuple[str, JobLane], int] = {}
        self._running_by_thread: dict[tuple[str, JobLane], int] = {}
        self._root = JobScope(self, self._next_scope_id(), JobScopeKind.SYSTEM, None, None, JobOwner())
        self._scopes[self._root.scope_id] = self._root

    # -- identity -----------------------------------------------------------

    def new_job_id(self) -> str:
        """Thread-safe sequential id, unique for this registry's lifetime."""
        with self._lock:
            self._id_seq += 1
            return f"job-{self._id_seq}"

    def root_scope(self) -> JobScope:
        return self._root

    def _next_scope_id(self) -> str:
        self._scope_seq += 1
        return f"scope-{self._scope_seq}"

    # -- registration -------------------------------------------------------

    def register(
        self,
        job: Job,
        *,
        scope: JobScope,
        lane: JobLane,
        admission: AdmissionPolicy,
    ) -> ScopedJobInfo:
        """Record a ``pending`` job and bind this registry's listener.

        Admission is not attempted until :meth:`start`; the listener binding
        happens here so a job that finishes immediately after ``start`` still
        releases its slot and updates the record.
        """
        with self._lock:
            self._require_open_scope_locked(scope)
            job_id = job.info().id
            if job_id in self._records:
                raise JobRegistrationError(f"job {job_id!r} is already registered")
            if job.info().state is not JobState.PENDING:
                raise JobRegistrationError(f"job {job_id!r} must be pending to register")
            record = _Record(
                job=job,
                scope=scope,
                owner=scope.owner,
                lane=lane,
                admission=admission,
                parent_job_id=scope.parent_job_id,
                queued_at=self._clock(),
            )
            self._records[job_id] = record
            info = self._build_info(record)
        job.add_listener(self)
        return info

    def start(self, job_id: str) -> ScopedJobInfo:
        """Attempt admission; with ``wait`` mode this blocks up to the queue
        timeout and raises :class:`JobAdmissionTimeout` on expiry."""
        with self._lock:
            record = self._records.get(job_id)
            if record is None:
                raise JobNotFound(job_id)
            if record.job.info().state is not JobState.PENDING:
                raise JobRegistrationError(f"job {job_id!r} is not pending")
            try:
                action = self._admission_decision_locked(record)
            except JobQueueFull:
                job = record.job
                queued_full = True
            else:
                queued_full = False
        if queued_full:
            job.cancel("queue full")
            raise JobQueueFull(job.info().id)
        return self._finish_admission(record, action)

    def submit(
        self,
        job: Job,
        *,
        scope: JobScope,
        lane: JobLane,
        admission: AdmissionPolicy,
    ) -> ScopedJobInfo:
        """Register and start in one call (a convenience for callers)."""
        self.register(job, scope=scope, lane=lane, admission=admission)
        return self.start(job.info().id)

    # -- lookup -------------------------------------------------------------

    def get(self, job_id: str) -> ScopedJobInfo | None:
        with self._lock:
            record = self._records.get(job_id)
            return self._build_info(record) if record is not None else None

    def list(self, query: JobQuery | None = None) -> tuple[ScopedJobInfo, ...]:
        with self._lock:
            return tuple(self._build_info(record) for record in self._records.values() if self._matches(record, query))

    def active_count(self, *, lane: JobLane | None = None, scope: JobScope | None = None) -> int:
        """Count pending and running jobs, optionally filtered."""
        with self._lock:
            count = 0
            for record in self._records.values():
                if record.job.info().state not in (JobState.PENDING, JobState.RUNNING):
                    continue
                if lane is not None and record.lane is not lane:
                    continue
                if scope is not None and not self._scope_contains_locked(scope, record.scope):
                    continue
                count += 1
            return count

    def unregister(self, job_id: str) -> None:
        """Drop a terminal job's record; non-terminal jobs are rejected."""
        with self._lock:
            record = self._records.get(job_id)
            if record is None:
                raise JobNotFound(job_id)
            if record.job.info().state not in TERMINAL_STATES:
                raise JobRegistrationError(f"job {job_id!r} is not terminal")
            del self._records[job_id]
        record.job.remove_listener(self)
