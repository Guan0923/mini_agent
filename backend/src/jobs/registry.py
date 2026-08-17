"""In-process job registry: registration, scopes, admission, and scheduling.

One registry owns one system root scope and all job records for a process.
It registers only ``pending`` jobs, binds its state listener before
:meth:`JobRegistry.start` so fast-completing jobs are never missed, admits
jobs against hierarchical per-lane limits, queues FIFO per owner, and
releases slot leases exactly once on terminal state.  The registry never
calls ``Job.start/cancel/close`` while holding its own lock.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .base import TERMINAL_STATES, Job, JobInfo, JobState, JobStateChange, JobStateListener
from .errors import (
    JobAdmissionRejected,
    JobAdmissionTimeout,
    JobNotFound,
    JobQueueFull,
    JobRegistrationError,
    JobScopeClosed,
)
from .scheduling import (
    AdmissionPolicy,
    JobLane,
    JobLimitPolicy,
    QueueMode,
    SlotLease,
    SlotMode,
)
from .scope import JobOwner, JobScope, JobScopeKind, _merge_owner

logger = logging.getLogger(__name__)

Clock = Any  # Callable[[], datetime]; kept loose to avoid a public alias here


@dataclass(frozen=True, slots=True)
class JobQuery:
    """Filter for registry/scope listings."""

    lanes: tuple[JobLane, ...] | None = None
    states: tuple[JobState, ...] | None = None


@dataclass(frozen=True, slots=True)
class ScopedJobInfo:
    """Public projection: module 1 ``JobInfo`` plus scheduling metadata.

    Owner identity is intentionally absent; it is internal control
    information used only for quota filtering.
    """

    info: JobInfo
    lane: JobLane
    scope_id: str
    scope_kind: JobScopeKind
    parent_job_id: str | None
    queued_at: datetime | None = None
    admitted_at: datetime | None = None
    slot_mode: SlotMode = SlotMode.COUNTED
    holds_slot: bool = False


@dataclass(frozen=True, slots=True)
class CloseReport:
    """Outcome of a scope close; never carries raw exception text."""

    closed: tuple[str, ...] = ()
    timed_out: tuple[str, ...] = ()
    failed: tuple[str, ...] = ()


class _Record:
    __slots__ = (
        "job",
        "scope",
        "owner",
        "lane",
        "admission",
        "parent_job_id",
        "lease",
        "queued_at",
        "admitted_at",
        "terminal",
    )

    def __init__(
        self,
        job: Job,
        scope: JobScope,
        owner: JobOwner,
        lane: JobLane,
        admission: AdmissionPolicy,
        parent_job_id: str | None,
        queued_at: datetime,
    ) -> None:
        self.job = job
        self.scope = scope
        self.owner = owner
        self.lane = lane
        self.admission = admission
        self.parent_job_id = parent_job_id
        self.lease: SlotLease | None = None
        self.queued_at = queued_at
        self.admitted_at: datetime | None = None
        self.terminal = False


class JobRegistry(JobStateListener):
    """Process-wide job control center built on the module 1 state machine."""

    def __init__(
        self,
        *,
        policy: JobLimitPolicy | None = None,
        clock: Any | None = None,
        history_limit: int = 1000,
        user_history_limit: int = 100,
    ) -> None:
        self._policy = policy or JobLimitPolicy.defaults()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._history_limit = history_limit
        self._user_history_limit = user_history_limit
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._id_seq = 0
        self._scope_seq = 0
        self._records: dict[str, _Record] = {}
        self._scopes: dict[str, JobScope] = {}
        self._closed_scopes: set[str] = set()
        self._queues: dict[JobLane, deque[str]] = {lane: deque() for lane in JobLane}
        self._running: dict[JobLane, int] = {lane: 0 for lane in JobLane}
        self._running_by_user: dict[tuple[str, JobLane], int] = {}
        self._running_by_runner: dict[tuple[str, JobLane], int] = {}
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

    def get_for_user(self, user_id: str, job_id: str) -> ScopedJobInfo | None:
        """Return a job only when its inherited owner belongs to ``user_id``."""
        with self._lock:
            record = self._records.get(job_id)
            if record is None or record.owner.user_id != user_id:
                return None
            return self._build_info(record)

    def list(self, query: JobQuery | None = None) -> tuple[ScopedJobInfo, ...]:
        with self._lock:
            return tuple(self._build_info(record) for record in self._records.values() if self._matches(record, query))

    def list_for_user(
        self, user_id: str, query: JobQuery | None = None, *, session_id: str | None = None
    ) -> tuple[ScopedJobInfo, ...]:
        """List only jobs whose effective owner is ``user_id``."""
        with self._lock:
            return tuple(
                self._build_info(record)
                for record in self._records.values()
                if record.owner.user_id == user_id
                and (session_id is None or record.owner.session_id == session_id)
                and self._matches(record, query)
            )

    def cancel_for_user(self, user_id: str, job_id: str) -> bool:
        """Request cancellation after enforcing the owner boundary."""
        with self._lock:
            record = self._records.get(job_id)
            if record is None or record.owner.user_id != user_id:
                return False
            job = record.job
        return job.cancel("user requested cancellation")

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

    # -- scope creation and close -------------------------------------------

    def _create_scope(
        self,
        parent: JobScope,
        kind: JobScopeKind,
        *,
        user_id: str | None,
        session_id: str | None,
        run_id: str | None,
        parent_job_id: str | None,
    ) -> JobScope:
        if kind is JobScopeKind.SYSTEM:
            raise ValueError("only the registry root may be a system scope")
        with self._lock:
            self._require_open_scope_locked(parent)
            owner = _merge_owner(parent.owner, user_id=user_id, session_id=session_id, run_id=run_id)
            scope = JobScope(self, self._next_scope_id(), kind, parent, parent_job_id, owner)
            self._scopes[scope.scope_id] = scope
            return scope

    def _is_scope_closed(self, scope: JobScope) -> bool:
        with self._lock:
            return scope.scope_id in self._closed_scopes

    def close_all(self, reason: str = "", timeout: float | None = None) -> CloseReport:
        """Close the root scope: every scope and job in the process."""
        return self._close_scope(self._root, timeout)

    def _close_scope(self, scope: JobScope, timeout: float | None) -> CloseReport:
        with self._lock:
            if scope.scope_id in self._closed_scopes:
                return CloseReport()
            scopes = self._collect_scopes_locked(scope)
            for candidate in scopes:
                self._closed_scopes.add(candidate.scope_id)
            pending: list[Job] = []
            running: list[tuple[int, Job]] = []
            for record in self._records.values():
                if record.scope not in scopes:
                    continue
                state = record.job.info().state
                if state is JobState.PENDING:
                    pending.append(record.job)
                elif state is JobState.RUNNING:
                    running.append((record.scope.depth, record.job))
        deadline = None if timeout is None else time.monotonic() + timeout
        closed: list[str] = []
        timed_out: list[str] = []
        failed: list[str] = []
        for job in pending:
            try:
                if job.info().state is JobState.PENDING:
                    job.cancel("scope closed")
                    closed.append(job.info().id)
            except Exception:
                failed.append(job.info().id)
        # Close running jobs deepest scope first, sharing the deadline.
        running.sort(key=lambda item: item[0], reverse=True)
        for _, job in running:
            try:
                remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
                if remaining is not None and remaining <= 0:
                    timed_out.append(job.info().id)
                    continue
                job.close(remaining)
                if job.info().state in TERMINAL_STATES:
                    closed.append(job.info().id)
                else:
                    timed_out.append(job.info().id)
            except Exception:
                failed.append(job.info().id)
        return CloseReport(tuple(closed), tuple(timed_out), tuple(failed))

    def _collect_scopes_locked(self, scope: JobScope) -> list[JobScope]:
        result = [scope]
        for candidate in self._scopes.values():
            if candidate is scope:
                continue
            current = candidate.parent
            while current is not None:
                if current is scope:
                    result.append(candidate)
                    break
                current = current.parent
        return result

    # -- scope-bound delegation ---------------------------------------------

    def _register_in_scope(
        self, scope: JobScope, job: Job, *, lane: JobLane, admission: AdmissionPolicy
    ) -> ScopedJobInfo:
        return self.register(job, scope=scope, lane=lane, admission=admission)

    def _submit_in_scope(
        self, scope: JobScope, job: Job, *, lane: JobLane, admission: AdmissionPolicy
    ) -> ScopedJobInfo:
        self.register(job, scope=scope, lane=lane, admission=admission)
        return self.start(job.info().id)

    def _start_in_scope(self, scope: JobScope, job_id: str) -> ScopedJobInfo:
        with self._lock:
            record = self._records.get(job_id)
            if record is None or not self._scope_contains_locked(scope, record.scope):
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

    def _get_in_scope(self, scope: JobScope, job_id: str) -> ScopedJobInfo | None:
        with self._lock:
            record = self._records.get(job_id)
            if record is None or not self._scope_contains_locked(scope, record.scope):
                return None
            return self._build_info(record)

    def _list_in_scope(self, scope: JobScope, query: JobQuery | None) -> tuple[ScopedJobInfo, ...]:
        with self._lock:
            return tuple(
                self._build_info(record)
                for record in self._records.values()
                if self._scope_contains_locked(scope, record.scope) and self._matches(record, query)
            )

    def _cancel_in_scope(self, scope: JobScope, job_id: str) -> bool:
        with self._lock:
            record = self._records.get(job_id)
            if record is None or not self._scope_contains_locked(scope, record.scope):
                return False
            job = record.job
        return job.cancel()

    # -- admission ----------------------------------------------------------

    def _admission_decision_locked(self, record: _Record) -> str:
        """Return 'admit', 'queue', or 'reject' under the current limits."""
        if record.admission.slot_mode is SlotMode.UNMETERED:
            return "admit"
        if record.admission.slot_mode is SlotMode.INHERIT and self._inherited_lease_locked(record) is not None:
            # Reuse the nearest ancestor's slot in the same lane; the
            # inherited slot is not counted again and bypasses quotas so a
            # parent holding the last slot can still run children.
            return "admit"
        if self._can_admit_locked(record):
            return "admit"
        if record.admission.queue_mode is QueueMode.REJECT:
            return "reject"
        self._check_queue_capacity_locked(record)
        return "queue"

    def _finish_admission(self, record: _Record, action: str) -> ScopedJobInfo:
        if action == "admit":
            with self._lock:
                self._admit_locked(record)
                to_start = [record.job]
                self._cond.notify_all()
            self._start_jobs(to_start)
            with self._lock:
                return self._build_info(record)
        if action == "reject":
            record.job.cancel("admission rejected")
            raise JobAdmissionRejected(record.job.info().id)
        # action == "queue"
        with self._lock:
            if record.job.info().state is not JobState.PENDING:
                # Concurrently cancelled between decision and enqueue.
                return self._build_info(record)
            self._queues[record.lane].append(record.job.info().id)
        return self._wait_for_admission(record)

    def _wait_for_admission(self, record: _Record) -> ScopedJobInfo:
        timeout = record.admission.queue_timeout_seconds
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            with self._lock:
                if record.admitted_at is not None:
                    return self._build_info(record)
                if record.job.info().state is not JobState.PENDING:
                    # Cancelled while queued.
                    return self._build_info(record)
                remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
                if remaining is not None and remaining <= 0:
                    expired = True
                else:
                    expired = False
                    self._cond.wait(remaining)
            if expired:
                record.job.cancel("queue timeout")
                raise JobAdmissionTimeout(record.job.info().id)

    def _can_admit_locked(self, record: _Record) -> bool:
        lane = record.lane
        if self._running[lane] >= self._policy.system[lane].max_running:
            return False
        user = record.owner.user_id
        if user is not None:
            key = (user, lane)
            if self._running_by_user.get(key, 0) >= self._policy.user[lane].max_running:
                return False
        runner_key = self._runner_key_locked(record.scope)
        if runner_key is not None:
            key = (runner_key, lane)
            if self._running_by_runner.get(key, 0) >= self._policy.runner[lane].max_running:
                return False
        return True

    def _check_queue_capacity_locked(self, record: _Record) -> None:
        lane = record.lane
        queue = self._queues[lane]
        if len(queue) >= self._policy.system[lane].max_queued:
            raise JobQueueFull(record.job.info().id)
        user = record.owner.user_id
        if user is not None:
            queued_by_user = sum(
                1 for job_id in queue if (r := self._records.get(job_id)) is not None and r.owner.user_id == user
            )
            if queued_by_user >= self._policy.user[lane].max_queued:
                raise JobQueueFull(record.job.info().id)
        runner_key = self._runner_key_locked(record.scope)
        if runner_key is not None:
            queued_by_runner = sum(
                1
                for job_id in queue
                if (r := self._records.get(job_id)) is not None and self._runner_key_locked(r.scope) == runner_key
            )
            if queued_by_runner >= self._policy.runner[lane].max_queued:
                raise JobQueueFull(record.job.info().id)

    def _admit_locked(self, record: _Record) -> None:
        lane = record.lane
        counted = True
        if record.admission.slot_mode is SlotMode.INHERIT:
            inherited = self._inherited_lease_locked(record)
            counted = inherited is None  # reuse the ancestor slot when present
        record.admitted_at = self._clock()
        if record.admission.slot_mode is SlotMode.UNMETERED:
            record.lease = SlotLease(lane, counted=False)
        else:
            record.lease = SlotLease(lane, counted=counted)
            if counted:
                self._running[lane] += 1
                user = record.owner.user_id
                if user is not None:
                    key = (user, lane)
                    self._running_by_user[key] = self._running_by_user.get(key, 0) + 1
                runner_key = self._runner_key_locked(record.scope)
                if runner_key is not None:
                    key = (runner_key, lane)
                    self._running_by_runner[key] = self._running_by_runner.get(key, 0) + 1

    def _inherited_lease_locked(self, record: _Record) -> SlotLease | None:
        current = record.parent_job_id
        while current is not None:
            parent = self._records.get(current)
            if parent is None:
                break
            lease = parent.lease
            if lease is not None and lease.lane is record.lane and not lease.released:
                return lease
            current = parent.parent_job_id
        return None

    def _runner_key_locked(self, scope: JobScope) -> str | None:
        current = scope
        while current is not None:
            if current.kind is JobScopeKind.RUNNER:
                return current.scope_id
            current = current.parent
        return None

    def _drain_queue_locked(self, lane: JobLane) -> list[Job]:
        queue = self._queues[lane]
        to_start: list[Job] = []
        admitted_users: set[str] = set()
        admitted_runners: set[str] = set()
        remaining: deque[str] = deque()
        while queue:
            job_id = queue.popleft()
            record = self._records.get(job_id)
            if record is None or record.job.info().state is not JobState.PENDING:
                continue
            user = record.owner.user_id
            if user is not None and user in admitted_users:
                remaining.append(job_id)
                continue
            runner_key = self._runner_key_locked(record.scope)
            if runner_key is not None and runner_key in admitted_runners:
                remaining.append(job_id)
                continue
            if not self._can_admit_locked(record):
                remaining.append(job_id)
                continue
            self._admit_locked(record)
            to_start.append(record.job)
            if user is not None:
                admitted_users.add(user)
            if runner_key is not None:
                admitted_runners.add(runner_key)
        self._queues[lane] = remaining
        return to_start

    # -- terminal handling --------------------------------------------------

    def on_job_state_change(self, change: JobStateChange) -> None:
        if change.job_info.state in TERMINAL_STATES:
            self._on_terminal(change.job_info.id)

    def _on_terminal(self, job_id: str) -> None:
        with self._lock:
            record = self._records.get(job_id)
            if record is None or record.terminal:
                return
            record.terminal = True
            queue = self._queues[record.lane]
            if job_id in queue:
                queue.remove(job_id)
            lease = record.lease
            if lease is not None and lease.release() and lease.counted:
                self._running[record.lane] -= 1
                user = record.owner.user_id
                if user is not None:
                    key = (user, record.lane)
                    self._running_by_user[key] -= 1
                runner_key = self._runner_key_locked(record.scope)
                if runner_key is not None:
                    key = (runner_key, record.lane)
                    self._running_by_runner[key] -= 1
            self._prune_history_locked()
            to_start = self._drain_queue_locked(record.lane)
            self._cond.notify_all()
        self._start_jobs(to_start)

    def _start_jobs(self, jobs: list[Job]) -> None:
        for job in jobs:
            try:
                job.start()
            except BaseException as exc:
                try:
                    job._mark_failed(exc)
                except Exception:
                    try:
                        job.cancel()
                    except Exception:
                        self._on_terminal(job.info().id)
                raise

    # -- history ------------------------------------------------------------

    def _prune_history_locked(self) -> None:
        terminal = [record for record in self._records.values() if record.terminal]
        if len(terminal) > self._history_limit:
            ordered = sorted(terminal, key=lambda record: record.queued_at or datetime.min.replace(tzinfo=UTC))
            for record in ordered[: len(terminal) - self._history_limit]:
                del self._records[record.job.info().id]
                record.job.remove_listener(self)
        by_user: dict[str, list[_Record]] = {}
        for record in self._records.values():
            if record.terminal and record.owner.user_id is not None:
                by_user.setdefault(record.owner.user_id, []).append(record)
        for records in by_user.values():
            if len(records) > self._user_history_limit:
                ordered = sorted(records, key=lambda record: record.queued_at or datetime.min.replace(tzinfo=UTC))
                for record in ordered[: len(records) - self._user_history_limit]:
                    del self._records[record.job.info().id]
                    record.job.remove_listener(self)

    # -- projection helpers -------------------------------------------------

    def _build_info(self, record: _Record) -> ScopedJobInfo:
        lease = record.lease
        return ScopedJobInfo(
            info=record.job.info(),
            lane=record.lane,
            scope_id=record.scope.scope_id,
            scope_kind=record.scope.kind,
            parent_job_id=record.parent_job_id,
            queued_at=record.queued_at,
            admitted_at=record.admitted_at,
            slot_mode=record.admission.slot_mode,
            holds_slot=lease is not None and lease.counted and not lease.released,
        )

    def _matches(self, record: _Record, query: JobQuery | None) -> bool:
        if query is None:
            return True
        if query.lanes is not None and record.lane not in query.lanes:
            return False
        if query.states is not None and record.job.info().state not in query.states:
            return False
        return True

    def _scope_contains_locked(self, ancestor: JobScope, scope: JobScope) -> bool:
        current = scope
        while current is not None:
            if current is ancestor:
                return True
            current = current.parent
        return False

    def _require_open_scope_locked(self, scope: JobScope) -> None:
        if scope.scope_id in self._closed_scopes:
            raise JobScopeClosed(scope.scope_id)


__all__ = [
    "CloseReport",
    "JobNotFound",
    "JobQuery",
    "JobRegistry",
    "JobRegistrationError",
    "ScopedJobInfo",
]
