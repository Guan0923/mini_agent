"""Session/thread-scoped job registry contracts for the local-only backend."""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime

import pytest

from backend.jobs import Job, JobKind, JobState
from backend.jobs.errors import JobNotFound, JobScopeClosed
from backend.jobs.registry import JobQuery, JobRegistry
from backend.jobs.scheduling import AdmissionPolicy, JobLane, JobLimitPolicy, LaneLimits, SlotMode
from backend.jobs.scope import JobOwner, JobScopeKind


class StubJob(Job):
    def __init__(self, job_id: str) -> None:
        super().__init__(job_id, JobKind.THREAD)

    def _request_cancel(self) -> None:
        return None


class AutoCompleteJob(StubJob):
    def start(self) -> None:
        super().start()
        self._mark_succeeded(exit_code=0)


def policy(*, system: int = 4, session: int = 2, thread: int = 1) -> JobLimitPolicy:
    def limits(value: int) -> dict[JobLane, LaneLimits]:
        return {lane: LaneLimits(max_running=value, max_queued=8) for lane in JobLane}

    return JobLimitPolicy(system=limits(system), session=limits(session), thread=limits(thread))


def registry(**kwargs) -> JobRegistry:
    return JobRegistry(policy=policy(), clock=lambda: datetime.now(UTC), **kwargs)


def wait_until(predicate, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("condition was not reached in time")


def test_ids_are_unique_under_concurrent_allocation() -> None:
    jobs = registry()
    values: list[str] = []
    lock = threading.Lock()

    def allocate() -> None:
        allocated = [jobs.new_job_id() for _ in range(20)]
        with lock:
            values.extend(allocated)

    threads = [threading.Thread(target=allocate) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(values) == len(set(values)) == 80


def test_scope_owner_is_session_thread_and_run_only() -> None:
    jobs = registry()
    session = jobs.root_scope().child(JobScopeKind.SESSION, session_id="session-1")
    thread = session.child(JobScopeKind.THREAD, thread_id="thread-1")
    run = thread.child(JobScopeKind.RUN, run_id="run-1")

    assert session.owner == JobOwner(session_id="session-1")
    assert thread.owner == JobOwner(session_id="session-1", thread_id="thread-1")
    assert run.owner == JobOwner(session_id="session-1", thread_id="thread-1", run_id="run-1")
    assert not hasattr(run.owner, "user_id")


def test_scope_rejects_owner_override_and_hides_siblings() -> None:
    jobs = registry()
    first = jobs.root_scope().child(JobScopeKind.SESSION, session_id="first")
    second = jobs.root_scope().child(JobScopeKind.SESSION, session_id="second")
    with pytest.raises(ValueError, match="session"):
        first.child(JobScopeKind.THREAD, session_id="second")

    job = AutoCompleteJob(jobs.new_job_id())
    first.submit(job, lane=JobLane.FOREGROUND, admission=AdmissionPolicy())
    assert first.get(job.info().id) is not None
    assert second.get(job.info().id) is None


def test_job_query_filters_by_session() -> None:
    jobs = registry()
    first = jobs.root_scope().child(JobScopeKind.SESSION, session_id="first")
    second = jobs.root_scope().child(JobScopeKind.SESSION, session_id="second")
    first.submit(AutoCompleteJob(jobs.new_job_id()), lane=JobLane.FOREGROUND, admission=AdmissionPolicy())
    second.submit(AutoCompleteJob(jobs.new_job_id()), lane=JobLane.FOREGROUND, admission=AdmissionPolicy())
    assert len(jobs.list(JobQuery(session_id="first"))) == 1


def test_thread_limit_queues_until_the_running_job_finishes() -> None:
    jobs = JobRegistry(policy=policy(system=2, session=2, thread=1))
    session = jobs.root_scope().child(JobScopeKind.SESSION, session_id="session-1")
    thread = session.child(JobScopeKind.THREAD, thread_id="thread-1")
    first = StubJob(jobs.new_job_id())
    thread.submit(first, lane=JobLane.FOREGROUND, admission=AdmissionPolicy())
    second = StubJob(jobs.new_job_id())
    jobs.register(second, scope=thread, lane=JobLane.FOREGROUND, admission=AdmissionPolicy())
    waiter = threading.Thread(target=lambda: jobs.start(second.info().id))
    waiter.start()
    wait_until(lambda: jobs.get(second.info().id).admitted_at is None)
    assert second.info().state is JobState.PENDING

    first._mark_succeeded()
    wait_until(lambda: second.info().state is JobState.RUNNING)
    second.cancel()
    waiter.join(2)


def test_session_limit_aggregates_across_threads() -> None:
    jobs = JobRegistry(policy=policy(system=2, session=1, thread=1))
    session = jobs.root_scope().child(JobScopeKind.SESSION, session_id="session-1")
    first_thread = session.child(JobScopeKind.THREAD, thread_id="thread-1")
    second_thread = session.child(JobScopeKind.THREAD, thread_id="thread-2")
    first = StubJob(jobs.new_job_id())
    first_thread.submit(first, lane=JobLane.FOREGROUND, admission=AdmissionPolicy())
    second = StubJob(jobs.new_job_id())
    jobs.register(second, scope=second_thread, lane=JobLane.FOREGROUND, admission=AdmissionPolicy())
    waiter = threading.Thread(target=lambda: jobs.start(second.info().id))
    waiter.start()
    wait_until(lambda: jobs.get(second.info().id).admitted_at is None)
    assert second.info().state is JobState.PENDING
    first._mark_succeeded()
    wait_until(lambda: second.info().state is JobState.RUNNING)
    second.cancel()
    waiter.join(2)


def test_inherited_child_reuses_parent_slot() -> None:
    jobs = JobRegistry(policy=policy(system=1, session=1, thread=1))
    thread = jobs.root_scope().child(JobScopeKind.THREAD, thread_id="thread-1")
    parent = StubJob(jobs.new_job_id())
    thread.submit(parent, lane=JobLane.FOREGROUND, admission=AdmissionPolicy())
    task = thread.child(JobScopeKind.TASK, parent_job_id=parent.info().id)
    child = StubJob(jobs.new_job_id())
    info = task.submit(
        child,
        lane=JobLane.FOREGROUND,
        admission=AdmissionPolicy(slot_mode=SlotMode.INHERIT),
    )
    assert info.info.state is JobState.RUNNING
    assert info.holds_slot is False


def test_scope_close_cancels_descendants_and_rejects_new_children() -> None:
    jobs = registry()
    session = jobs.root_scope().child(JobScopeKind.SESSION, session_id="session-1")
    pending = StubJob(jobs.new_job_id())
    session.register(pending, lane=JobLane.FOREGROUND, admission=AdmissionPolicy())
    report = session.close(timeout=0.1)
    assert pending.info().state is JobState.CANCELLED
    assert pending.info().id in report.closed
    with pytest.raises(JobScopeClosed):
        session.child(JobScopeKind.THREAD, thread_id="late")


def test_session_history_limit_prunes_only_old_terminal_jobs() -> None:
    jobs = registry(history_limit=10, session_history_limit=2)
    session = jobs.root_scope().child(JobScopeKind.SESSION, session_id="session-1")
    completed = [AutoCompleteJob(jobs.new_job_id()) for _ in range(3)]
    for job in completed:
        session.submit(job, lane=JobLane.FOREGROUND, admission=AdmissionPolicy())
    assert jobs.get(completed[0].info().id) is None
    assert jobs.get(completed[1].info().id) is not None


def test_unknown_job_is_not_visible_inside_scope() -> None:
    jobs = registry()
    session = jobs.root_scope().child(JobScopeKind.SESSION, session_id="session-1")
    assert session.get("missing") is None
    with pytest.raises(JobNotFound):
        jobs.start("missing")
