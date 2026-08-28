"""Scope creation, containment, scoped delegation, and close operations."""

from __future__ import annotations

import time

from ..base import TERMINAL_STATES, Job, JobState
from ..errors import JobNotFound, JobQueueFull, JobRegistrationError
from ..scheduling import AdmissionPolicy, JobLane
from ..scope import JobScope, JobScopeKind, _merge_owner
from .models import CloseReport, JobQuery, ScopedJobInfo


class _ScopeRegistryMixin:
    # -- scope creation and close -------------------------------------------

    def _create_scope(
        self,
        parent: JobScope,
        kind: JobScopeKind,
        *,
        session_id: str | None,
        thread_id: str | None,
        run_id: str | None,
        parent_job_id: str | None,
    ) -> JobScope:
        if kind is JobScopeKind.SYSTEM:
            raise ValueError("only the registry root may be a system scope")
        with self._lock:
            self._require_open_scope_locked(parent)
            owner = _merge_owner(parent.owner, session_id=session_id, thread_id=thread_id, run_id=run_id)
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
