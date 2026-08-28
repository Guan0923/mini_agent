"""Hierarchical lane admission, queueing, and slot accounting."""

from __future__ import annotations

import time
from collections import deque

from ..base import Job, JobState
from ..errors import JobAdmissionRejected, JobAdmissionTimeout, JobQueueFull
from ..scheduling import JobLane, QueueMode, SlotLease, SlotMode
from ..scope import JobScope, JobScopeKind
from .models import ScopedJobInfo, _Record


class _AdmissionRegistryMixin:
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
        session = record.owner.session_id
        if session is not None:
            key = (session, lane)
            if self._running_by_session.get(key, 0) >= self._policy.session[lane].max_running:
                return False
        thread_key = self._thread_key_locked(record.scope)
        if thread_key is not None:
            key = (thread_key, lane)
            if self._running_by_thread.get(key, 0) >= self._policy.thread[lane].max_running:
                return False
        return True

    def _check_queue_capacity_locked(self, record: _Record) -> None:
        lane = record.lane
        queue = self._queues[lane]
        if len(queue) >= self._policy.system[lane].max_queued:
            raise JobQueueFull(record.job.info().id)
        session = record.owner.session_id
        if session is not None:
            queued_by_session = sum(
                1 for job_id in queue if (r := self._records.get(job_id)) is not None and r.owner.session_id == session
            )
            if queued_by_session >= self._policy.session[lane].max_queued:
                raise JobQueueFull(record.job.info().id)
        thread_key = self._thread_key_locked(record.scope)
        if thread_key is not None:
            queued_by_thread = sum(
                1
                for job_id in queue
                if (r := self._records.get(job_id)) is not None and self._thread_key_locked(r.scope) == thread_key
            )
            if queued_by_thread >= self._policy.thread[lane].max_queued:
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
                session = record.owner.session_id
                if session is not None:
                    key = (session, lane)
                    self._running_by_session[key] = self._running_by_session.get(key, 0) + 1
                thread_key = self._thread_key_locked(record.scope)
                if thread_key is not None:
                    key = (thread_key, lane)
                    self._running_by_thread[key] = self._running_by_thread.get(key, 0) + 1

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

    def _thread_key_locked(self, scope: JobScope) -> str | None:
        current = scope
        while current is not None:
            if current.kind is JobScopeKind.THREAD:
                return current.scope_id
            current = current.parent
        return None

    def _drain_queue_locked(self, lane: JobLane) -> list[Job]:
        queue = self._queues[lane]
        to_start: list[Job] = []
        admitted_sessions: set[str] = set()
        admitted_threads: set[str] = set()
        remaining: deque[str] = deque()
        while queue:
            job_id = queue.popleft()
            record = self._records.get(job_id)
            if record is None or record.job.info().state is not JobState.PENDING:
                continue
            session = record.owner.session_id
            if session is not None and session in admitted_sessions:
                remaining.append(job_id)
                continue
            thread_key = self._thread_key_locked(record.scope)
            if thread_key is not None and thread_key in admitted_threads:
                remaining.append(job_id)
                continue
            if not self._can_admit_locked(record):
                remaining.append(job_id)
                continue
            self._admit_locked(record)
            to_start.append(record.job)
            if session is not None:
                admitted_sessions.add(session)
            if thread_key is not None:
                admitted_threads.add(thread_key)
        self._queues[lane] = remaining
        return to_start
