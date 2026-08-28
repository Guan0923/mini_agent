"""Terminal callbacks, queue draining, history pruning, and projections."""

from __future__ import annotations

from datetime import UTC, datetime

from ..base import TERMINAL_STATES, Job, JobStateChange
from ..errors import JobScopeClosed
from ..scope import JobScope
from .models import JobQuery, ScopedJobInfo, _Record


class _LifecycleRegistryMixin:
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
                session = record.owner.session_id
                if session is not None:
                    key = (session, record.lane)
                    self._running_by_session[key] -= 1
                thread_key = self._thread_key_locked(record.scope)
                if thread_key is not None:
                    key = (thread_key, record.lane)
                    self._running_by_thread[key] -= 1
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
        by_session: dict[str, list[_Record]] = {}
        for record in self._records.values():
            if record.terminal and record.owner.session_id is not None:
                by_session.setdefault(record.owner.session_id, []).append(record)
        for records in by_session.values():
            if len(records) > self._session_history_limit:
                ordered = sorted(records, key=lambda record: record.queued_at or datetime.min.replace(tzinfo=UTC))
                for record in ordered[: len(records) - self._session_history_limit]:
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
        if query.session_id is not None and record.owner.session_id != query.session_id:
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
