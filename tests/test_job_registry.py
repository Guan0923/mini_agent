"""Registry, scope, and scheduling tests for ``backend.jobs``.

Covers job ID allocation, registration rules, scope ownership and access
boundaries, layered lane limits, admission/queue semantics, slot leases,
scope close propagation, and terminal history pruning.
"""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime, timedelta

import pytest

from backend.jobs import Job, JobKind, JobState, JobStateError
from backend.jobs.errors import (
    JobAdmissionRejected,
    JobAdmissionTimeout,
    JobNotFound,
    JobQueueFull,
    JobRegistrationError,
    JobScopeClosed,
)
from backend.jobs.registry import JobQuery, JobRegistry, ScopedJobInfo
from backend.jobs.scheduling import (
    AdmissionPolicy,
    JobLane,
    JobLimitPolicy,
    LaneLimits,
    QueueMode,
    SlotMode,
)
from backend.jobs.scope import JobOwner, JobScope, JobScopeKind


class StubJob(Job):
    """A carrier that never finishes on its own; cancellation is recorded."""

    def __init__(self, job_id: str, **kwargs: object) -> None:
        super().__init__(job_id, JobKind.THREAD, **kwargs)  # type: ignore[arg-type]
        self.cancel_signals = 0

    def _request_cancel(self) -> None:
        self.cancel_signals += 1


class AutoCompleteJob(StubJob):
    """Finishes successfully immediately after start."""

    def start(self) -> None:
        super().start()
        self._mark_succeeded(exit_code=0)


class FailingStartJob(StubJob):
    """Raises from start like a carrier launch failure."""

    def start(self) -> None:
        super().start()
        raise RuntimeError("carrier launch failed")


class CancellableJob(StubJob):
    """Stops promptly when cancellation is requested."""

    def _request_cancel(self) -> None:
        super()._request_cancel()
        self._mark_cancelled()


class FakeClock:
    def __init__(self) -> None:
        self.now = datetime(2025, 1, 1, tzinfo=UTC)

    def __call__(self) -> datetime:
        self.now += timedelta(milliseconds=1)
        return self.now


def small_policy(*, running: int = 1, queued: int = 2) -> JobLimitPolicy:
    limits = {lane: LaneLimits(max_running=running, max_queued=queued) for lane in JobLane}
    return JobLimitPolicy(system=limits, user=limits, runner=limits)


def make_registry(*, running: int = 1, queued: int = 2, **kwargs: object) -> JobRegistry:
    kwargs.setdefault("policy", small_policy(running=running, queued=queued))
    kwargs.setdefault("clock", FakeClock())
    return JobRegistry(**kwargs)


def wait_until(predicate, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("condition was not reached in time")


def start_async(
    registry: JobRegistry, job: Job, *, scope: JobScope, lane: JobLane, admission: AdmissionPolicy
) -> tuple[threading.Thread, list[ScopedJobInfo]]:
    """Register and start a job in a worker thread; used when admission would
    block the caller."""
    registry.register(job, scope=scope, lane=lane, admission=admission)
    outcomes: list[ScopedJobInfo] = []
    thread = threading.Thread(target=lambda: outcomes.append(registry.start(job._id)))
    thread.start()
    return thread, outcomes


def policy_with(*, system_running: int = 2, user_running: int = 1, runner_running: int = 1) -> JobLimitPolicy:
    return JobLimitPolicy(
        system={lane: LaneLimits(max_running=system_running, max_queued=4) for lane in JobLane},
        user={lane: LaneLimits(max_running=user_running, max_queued=4) for lane in JobLane},
        runner={lane: LaneLimits(max_running=runner_running, max_queued=4) for lane in JobLane},
    )


class TestJobIds:
    def test_ids_are_sequential_and_unique(self) -> None:
        registry = make_registry()
        assert [registry.new_job_id() for _ in range(5)] == ["job-1", "job-2", "job-3", "job-4", "job-5"]

    def test_concurrent_allocation_is_unique(self) -> None:
        registry = make_registry()
        produced: list[str] = []
        lock = threading.Lock()

        def allocate() -> None:
            for _ in range(200):
                with lock:
                    produced.append(registry.new_job_id())

        threads = [threading.Thread(target=allocate) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert len(produced) == 800
        assert len(set(produced)) == 800


class TestRegistration:
    def test_register_returns_scoped_info(self) -> None:
        registry = make_registry()
        job = StubJob(registry.new_job_id())
        info = registry.register(job, scope=registry.root_scope(), lane=JobLane.FOREGROUND, admission=AdmissionPolicy())
        assert info.info.state is JobState.PENDING
        assert info.lane is JobLane.FOREGROUND
        assert info.scope_id == registry.root_scope().scope_id

    def test_duplicate_register_rejected(self) -> None:
        registry = make_registry()
        job = StubJob(registry.new_job_id())
        registry.register(job, scope=registry.root_scope(), lane=JobLane.FOREGROUND, admission=AdmissionPolicy())
        with pytest.raises(JobRegistrationError):
            registry.register(job, scope=registry.root_scope(), lane=JobLane.FOREGROUND, admission=AdmissionPolicy())

    def test_register_non_pending_job_rejected(self) -> None:
        registry = make_registry()
        job = StubJob(registry.new_job_id())
        job.start()
        with pytest.raises(JobRegistrationError):
            registry.register(job, scope=registry.root_scope(), lane=JobLane.FOREGROUND, admission=AdmissionPolicy())

    def test_register_on_closed_scope_rejected(self) -> None:
        registry = make_registry()
        scope = registry.root_scope().child(JobScopeKind.USER, user_id="u1")
        scope.close()
        with pytest.raises(JobScopeClosed):
            registry.register(
                StubJob(registry.new_job_id()), scope=scope, lane=JobLane.FOREGROUND, admission=AdmissionPolicy()
            )

    def test_unregister_terminal_job_removes_record(self) -> None:
        registry = make_registry()
        job = AutoCompleteJob(registry.new_job_id())
        registry.submit(job, scope=registry.root_scope(), lane=JobLane.FOREGROUND, admission=AdmissionPolicy())
        registry.unregister(job._id)
        assert registry.get(job._id) is None

    def test_unregister_running_job_rejected(self) -> None:
        registry = make_registry()
        job = StubJob(registry.new_job_id())
        registry.submit(job, scope=registry.root_scope(), lane=JobLane.FOREGROUND, admission=AdmissionPolicy())
        with pytest.raises(JobRegistrationError):
            registry.unregister(job._id)

    def test_unregister_unknown_job_rejected(self) -> None:
        registry = make_registry()
        with pytest.raises(JobNotFound):
            registry.unregister("job-missing")


class TestScopes:
    def test_root_scope_is_system(self) -> None:
        registry = make_registry()
        root = registry.root_scope()
        assert root.kind is JobScopeKind.SYSTEM
        assert root.parent is None
        assert root.owner == JobOwner()

    def test_child_inherits_and_extends_owner(self) -> None:
        registry = make_registry()
        user = registry.root_scope().child(JobScopeKind.USER, user_id="u1")
        runner = user.child(JobScopeKind.RUNNER, session_id="s1")
        run = runner.child(JobScopeKind.RUN, run_id="r1")
        assert user.owner == JobOwner(user_id="u1")
        assert runner.owner == JobOwner(user_id="u1", session_id="s1")
        assert run.owner == JobOwner(user_id="u1", session_id="s1", run_id="r1")

    def test_child_cannot_override_inherited_owner(self) -> None:
        registry = make_registry()
        user = registry.root_scope().child(JobScopeKind.USER, user_id="u1")
        with pytest.raises(ValueError):
            user.child(JobScopeKind.RUNNER, user_id="u2")
        runner = user.child(JobScopeKind.RUNNER, session_id="s1")
        with pytest.raises(ValueError):
            runner.child(JobScopeKind.RUN, session_id="other")
        with pytest.raises(ValueError):
            runner.child(JobScopeKind.RUN, run_id="r1", session_id="other")

    def test_scope_get_only_sees_descendants(self) -> None:
        registry = make_registry()
        user_a = registry.root_scope().child(JobScopeKind.USER, user_id="a")
        user_b = registry.root_scope().child(JobScopeKind.USER, user_id="b")
        job_a = AutoCompleteJob(registry.new_job_id())
        job_b = AutoCompleteJob(registry.new_job_id())
        registry.submit(job_a, scope=user_a, lane=JobLane.FOREGROUND, admission=AdmissionPolicy())
        registry.submit(job_b, scope=user_b, lane=JobLane.FOREGROUND, admission=AdmissionPolicy())
        assert user_a.get(job_a._id) is not None
        assert user_a.get(job_b._id) is None
        assert user_b.get(job_a._id) is None

    def test_sibling_scopes_are_isolated(self) -> None:
        registry = make_registry()
        user_a = registry.root_scope().child(JobScopeKind.USER, user_id="a")
        user_b = registry.root_scope().child(JobScopeKind.USER, user_id="b")
        registry.submit(
            AutoCompleteJob(registry.new_job_id()),
            scope=user_a,
            lane=JobLane.FOREGROUND,
            admission=AdmissionPolicy(),
        )
        assert len(user_b.list()) == 0
        assert len(registry.root_scope().list()) == 1

    def test_scope_list_filters_by_query(self) -> None:
        registry = make_registry()
        scope = registry.root_scope().child(JobScopeKind.USER, user_id="u1")
        registry.submit(
            AutoCompleteJob(registry.new_job_id()), scope=scope, lane=JobLane.FOREGROUND, admission=AdmissionPolicy()
        )
        registry.submit(
            AutoCompleteJob(registry.new_job_id()), scope=scope, lane=JobLane.SERVICE, admission=AdmissionPolicy()
        )
        assert len(scope.list(JobQuery(lanes=(JobLane.SERVICE,)))) == 1

    def test_closed_scope_rejects_new_children(self) -> None:
        registry = make_registry()
        user = registry.root_scope().child(JobScopeKind.USER, user_id="u1")
        user.close()
        with pytest.raises(JobScopeClosed):
            user.child(JobScopeKind.RUNNER)

    def test_owner_is_not_exposed_in_public_projection(self) -> None:
        registry = make_registry()
        scope = registry.root_scope().child(JobScopeKind.USER, user_id="u1", session_id="s1")
        job = AutoCompleteJob(registry.new_job_id())
        info = registry.submit(job, scope=scope, lane=JobLane.FOREGROUND, admission=AdmissionPolicy())
        assert not hasattr(info, "owner")
        assert "u1" not in repr(info)


class TestAdmission:
    def test_submit_runs_immediately_when_slot_available(self) -> None:
        registry = make_registry()
        job = StubJob(registry.new_job_id())
        info = registry.submit(job, scope=registry.root_scope(), lane=JobLane.FOREGROUND, admission=AdmissionPolicy())
        assert info.info.state is JobState.RUNNING
        assert info.admitted_at is not None
        assert info.holds_slot

    def test_second_job_queues_then_runs_after_first_completes(self) -> None:
        registry = make_registry()
        first = StubJob(registry.new_job_id())
        registry.submit(first, scope=registry.root_scope(), lane=JobLane.FOREGROUND, admission=AdmissionPolicy())
        second = AutoCompleteJob(registry.new_job_id())
        thread, outcomes = start_async(
            registry, second, scope=registry.root_scope(), lane=JobLane.FOREGROUND, admission=AdmissionPolicy()
        )
        try:
            wait_until(
                lambda: (
                    (info := registry.get(second._id)) is not None
                    and info.queued_at is not None
                    and info.admitted_at is None
                )
            )
            assert second.info().state is JobState.PENDING
            first._mark_succeeded()
            thread.join(2)
            assert not thread.is_alive()
            assert outcomes[0].info.state is JobState.SUCCEEDED
            assert registry.active_count() == 0
        finally:
            second.cancel()

    def test_reject_mode_raises_when_no_slot(self) -> None:
        registry = make_registry()
        first = StubJob(registry.new_job_id())
        registry.submit(first, scope=registry.root_scope(), lane=JobLane.FOREGROUND, admission=AdmissionPolicy())
        second = StubJob(registry.new_job_id())
        with pytest.raises(JobAdmissionRejected):
            registry.submit(
                second,
                scope=registry.root_scope(),
                lane=JobLane.FOREGROUND,
                admission=AdmissionPolicy(queue_mode=QueueMode.REJECT),
            )
        assert second.info().state is JobState.CANCELLED

    def test_queue_full_raises(self) -> None:
        registry = make_registry(queued=1)
        first = StubJob(registry.new_job_id())
        registry.submit(first, scope=registry.root_scope(), lane=JobLane.FOREGROUND, admission=AdmissionPolicy())
        second = StubJob(registry.new_job_id())
        thread, _outcomes = start_async(
            registry, second, scope=registry.root_scope(), lane=JobLane.FOREGROUND, admission=AdmissionPolicy()
        )
        try:
            wait_until(
                lambda: (
                    (info := registry.get(second._id)) is not None
                    and info.queued_at is not None
                    and info.admitted_at is None
                )
            )
            third = StubJob(registry.new_job_id())
            with pytest.raises(JobQueueFull):
                registry.submit(
                    third, scope=registry.root_scope(), lane=JobLane.FOREGROUND, admission=AdmissionPolicy()
                )
            assert third.info().state is JobState.CANCELLED
        finally:
            first._mark_succeeded()
            second.cancel()
            thread.join(2)

    def test_queue_timeout_raises_and_cancels(self) -> None:
        registry = make_registry()
        first = StubJob(registry.new_job_id())
        registry.submit(first, scope=registry.root_scope(), lane=JobLane.FOREGROUND, admission=AdmissionPolicy())
        second = StubJob(registry.new_job_id())
        with pytest.raises(JobAdmissionTimeout):
            registry.submit(
                second,
                scope=registry.root_scope(),
                lane=JobLane.FOREGROUND,
                admission=AdmissionPolicy(queue_timeout_seconds=0.05),
            )
        assert second.info().state is JobState.CANCELLED

    def test_cancel_while_queued_removes_job_from_queue(self) -> None:
        registry = make_registry()
        first = StubJob(registry.new_job_id())
        registry.submit(first, scope=registry.root_scope(), lane=JobLane.FOREGROUND, admission=AdmissionPolicy())
        second = StubJob(registry.new_job_id())
        outcomes: list[ScopedJobInfo] = []

        def submit_second() -> None:
            outcomes.append(
                registry.submit(
                    second, scope=registry.root_scope(), lane=JobLane.FOREGROUND, admission=AdmissionPolicy()
                )
            )

        thread = threading.Thread(target=submit_second)
        thread.start()
        try:
            wait_until(lambda: registry.get(second._id) is not None and registry.get(second._id).queued_at is not None)
            assert second.info().state is JobState.PENDING
            second.cancel()
            thread.join(2)
            assert not thread.is_alive()
            assert outcomes[0].info.state is JobState.CANCELLED
        finally:
            second.cancel()

    def test_system_limit_enforced_per_lane(self) -> None:
        policy = small_policy(running=1)
        registry = JobRegistry(policy=policy, clock=FakeClock())
        scope = registry.root_scope()
        first = StubJob(registry.new_job_id())
        registry.submit(first, scope=scope, lane=JobLane.FOREGROUND, admission=AdmissionPolicy())
        second = StubJob(registry.new_job_id())
        info = registry.register(second, scope=scope, lane=JobLane.FOREGROUND, admission=AdmissionPolicy())
        assert info.info.state is JobState.PENDING
        assert info.queued_at is not None
        assert info.admitted_at is None

    def test_user_limit_aggregates_across_runners(self) -> None:
        registry = JobRegistry(policy=policy_with(), clock=FakeClock())
        user = registry.root_scope().child(JobScopeKind.USER, user_id="u1")
        runner_a = user.child(JobScopeKind.RUNNER, session_id="s1")
        runner_b = user.child(JobScopeKind.RUNNER, session_id="s2")
        first = StubJob(registry.new_job_id())
        registry.submit(first, scope=runner_a, lane=JobLane.FOREGROUND, admission=AdmissionPolicy())
        second = StubJob(registry.new_job_id())
        thread, _outcomes = start_async(
            registry, second, scope=runner_b, lane=JobLane.FOREGROUND, admission=AdmissionPolicy()
        )
        try:
            # The second runner of the same user is blocked by the user quota.
            wait_until(
                lambda: (
                    (info := registry.get(second._id)) is not None
                    and info.queued_at is not None
                    and info.admitted_at is None
                )
            )
            assert second.info().state is JobState.PENDING
            # A different user can still run concurrently.
            other = registry.root_scope().child(JobScopeKind.USER, user_id="u2")
            third = AutoCompleteJob(registry.new_job_id())
            ran = registry.submit(third, scope=other, lane=JobLane.FOREGROUND, admission=AdmissionPolicy())
            assert ran.info.state is JobState.SUCCEEDED
        finally:
            first._mark_succeeded()
            second.cancel()
            thread.join(2)

    def test_runner_limit_enforced(self) -> None:
        registry = JobRegistry(policy=policy_with(user_running=2), clock=FakeClock())
        runner_a = (
            registry.root_scope().child(JobScopeKind.USER, user_id="u1").child(JobScopeKind.RUNNER, session_id="s1")
        )
        runner_b = (
            registry.root_scope().child(JobScopeKind.USER, user_id="u1").child(JobScopeKind.RUNNER, session_id="s2")
        )
        first = StubJob(registry.new_job_id())
        registry.submit(first, scope=runner_a, lane=JobLane.FOREGROUND, admission=AdmissionPolicy())
        second = StubJob(registry.new_job_id())
        thread, _outcomes = start_async(
            registry, second, scope=runner_a, lane=JobLane.FOREGROUND, admission=AdmissionPolicy()
        )
        try:
            # Runner a is full; a second job on the same runner queues.
            wait_until(
                lambda: (
                    (info := registry.get(second._id)) is not None
                    and info.queued_at is not None
                    and info.admitted_at is None
                )
            )
            # A sibling runner of the same user still fits the user quota.
            third = StubJob(registry.new_job_id())
            ran = registry.submit(third, scope=runner_b, lane=JobLane.FOREGROUND, admission=AdmissionPolicy())
            assert ran.info.state is JobState.RUNNING
        finally:
            first._mark_succeeded()
            second.cancel()
            thread.join(2)

    def test_lanes_do_not_block_each_other(self) -> None:
        registry = make_registry()
        scope = registry.root_scope()
        service = StubJob(registry.new_job_id())
        registry.submit(service, scope=scope, lane=JobLane.SERVICE, admission=AdmissionPolicy())
        foreground = AutoCompleteJob(registry.new_job_id())
        info = registry.submit(foreground, scope=scope, lane=JobLane.FOREGROUND, admission=AdmissionPolicy())
        assert info.info.state is JobState.SUCCEEDED

    def test_fifo_within_owner(self) -> None:
        registry = make_registry()
        user = registry.root_scope().child(JobScopeKind.USER, user_id="u1")
        first = StubJob(registry.new_job_id())
        registry.submit(first, scope=user, lane=JobLane.FOREGROUND, admission=AdmissionPolicy())
        second = StubJob(registry.new_job_id())
        third = StubJob(registry.new_job_id())
        thread_second, _ = start_async(
            registry, second, scope=user, lane=JobLane.FOREGROUND, admission=AdmissionPolicy()
        )
        thread_third, _ = start_async(registry, third, scope=user, lane=JobLane.FOREGROUND, admission=AdmissionPolicy())
        try:
            wait_until(
                lambda: (
                    (info := registry.get(second._id)) is not None
                    and info.queued_at is not None
                    and info.admitted_at is None
                    and (third_info := registry.get(third._id)) is not None
                    and third_info.queued_at is not None
                )
            )
            first._mark_succeeded()
            wait_until(lambda: second.info().state is JobState.RUNNING)
            assert third.info().state is JobState.PENDING
            second._mark_succeeded()
            wait_until(lambda: third.info().state is JobState.RUNNING)
            thread_second.join(2)
            thread_third.join(2)
        finally:
            for job in (first, second, third):
                job.cancel()
            thread_second.join(2)
            thread_third.join(2)

    def test_blocked_owner_head_does_not_block_other_users(self) -> None:
        registry = JobRegistry(policy=policy_with(), clock=FakeClock())
        user_a = registry.root_scope().child(JobScopeKind.USER, user_id="a")
        user_b = registry.root_scope().child(JobScopeKind.USER, user_id="b")
        first = StubJob(registry.new_job_id())
        registry.submit(first, scope=user_a, lane=JobLane.FOREGROUND, admission=AdmissionPolicy())
        second = StubJob(registry.new_job_id())
        thread, _outcomes = start_async(
            registry, second, scope=user_a, lane=JobLane.FOREGROUND, admission=AdmissionPolicy()
        )
        try:
            # User a's queued head is blocked by its own quota; user b's job
            # must still use the free system slot.
            wait_until(
                lambda: (
                    (info := registry.get(second._id)) is not None
                    and info.queued_at is not None
                    and info.admitted_at is None
                )
            )
            third = AutoCompleteJob(registry.new_job_id())
            ran = registry.submit(third, scope=user_b, lane=JobLane.FOREGROUND, admission=AdmissionPolicy())
            assert ran.info.state is JobState.SUCCEEDED
            assert second.info().state is JobState.PENDING
        finally:
            first._mark_succeeded()
            second.cancel()
            thread.join(2)

    def test_active_count_filters_by_lane_and_scope(self) -> None:
        registry = make_registry()
        user = registry.root_scope().child(JobScopeKind.USER, user_id="u1")
        registry.submit(
            StubJob(registry.new_job_id()), scope=user, lane=JobLane.FOREGROUND, admission=AdmissionPolicy()
        )
        registry.submit(StubJob(registry.new_job_id()), scope=user, lane=JobLane.SERVICE, admission=AdmissionPolicy())
        assert registry.active_count() == 2
        assert registry.active_count(lane=JobLane.FOREGROUND) == 1
        assert registry.active_count(scope=user) == 2


class TestSlotModes:
    def test_counted_occupies_slot(self) -> None:
        registry = make_registry()
        job = StubJob(registry.new_job_id())
        info = registry.submit(job, scope=registry.root_scope(), lane=JobLane.FOREGROUND, admission=AdmissionPolicy())
        assert info.holds_slot
        assert info.slot_mode is SlotMode.COUNTED

    def test_inherit_reuses_ancestor_slot(self) -> None:
        registry = make_registry()
        scope = registry.root_scope()
        parent = StubJob(registry.new_job_id())
        registry.submit(parent, scope=scope, lane=JobLane.FOREGROUND, admission=AdmissionPolicy())
        child_scope = scope.child(JobScopeKind.TASK, parent_job_id=parent._id)
        child = StubJob(registry.new_job_id())
        info = registry.submit(
            child,
            scope=child_scope,
            lane=JobLane.FOREGROUND,
            admission=AdmissionPolicy(slot_mode=SlotMode.INHERIT),
        )
        # The child reuses the parent's only slot instead of queueing.
        assert info.info.state is JobState.RUNNING
        assert info.parent_job_id == parent._id
        assert registry.active_count() == 2

    def test_inherit_without_ancestor_slot_falls_back_to_counted(self) -> None:
        registry = make_registry()
        job = StubJob(registry.new_job_id())
        info = registry.submit(
            job,
            scope=registry.root_scope(),
            lane=JobLane.FOREGROUND,
            admission=AdmissionPolicy(slot_mode=SlotMode.INHERIT),
        )
        assert info.info.state is JobState.RUNNING
        assert info.holds_slot

    def test_unmetered_does_not_occupy_slot(self) -> None:
        registry = make_registry()
        occupied = StubJob(registry.new_job_id())
        registry.submit(occupied, scope=registry.root_scope(), lane=JobLane.FOREGROUND, admission=AdmissionPolicy())
        watcher = StubJob(registry.new_job_id())
        info = registry.submit(
            watcher,
            scope=registry.root_scope(),
            lane=JobLane.FOREGROUND,
            admission=AdmissionPolicy(slot_mode=SlotMode.UNMETERED),
        )
        assert info.info.state is JobState.RUNNING
        assert not info.holds_slot

    def test_nested_inherit_does_not_deadlock(self) -> None:
        registry = make_registry(running=1)
        scope = registry.root_scope().child(JobScopeKind.USER, user_id="u1")
        parent = StubJob(registry.new_job_id())
        registry.submit(parent, scope=scope, lane=JobLane.FOREGROUND, admission=AdmissionPolicy())
        child_scope = scope.child(JobScopeKind.TASK, parent_job_id=parent._id)
        child = StubJob(registry.new_job_id())
        info = registry.submit(
            child,
            scope=child_scope,
            lane=JobLane.FOREGROUND,
            admission=AdmissionPolicy(slot_mode=SlotMode.INHERIT),
        )
        assert info.info.state is JobState.RUNNING


class TestSlotRelease:
    def test_fast_completion_releases_slot_once(self) -> None:
        registry = make_registry()
        fast = AutoCompleteJob(registry.new_job_id())
        registry.submit(fast, scope=registry.root_scope(), lane=JobLane.FOREGROUND, admission=AdmissionPolicy())
        wait_until(lambda: fast.info().state is JobState.SUCCEEDED)
        assert registry.active_count() == 0
        next_job = AutoCompleteJob(registry.new_job_id())
        info = registry.submit(
            next_job, scope=registry.root_scope(), lane=JobLane.FOREGROUND, admission=AdmissionPolicy()
        )
        assert info.info.state is JobState.SUCCEEDED

    def test_cancel_releases_slot_once(self) -> None:
        registry = make_registry()
        job = StubJob(registry.new_job_id())
        registry.submit(job, scope=registry.root_scope(), lane=JobLane.FOREGROUND, admission=AdmissionPolicy())
        job.cancel()
        job._mark_cancelled()
        assert registry.active_count() == 0
        next_job = AutoCompleteJob(registry.new_job_id())
        info = registry.submit(
            next_job, scope=registry.root_scope(), lane=JobLane.FOREGROUND, admission=AdmissionPolicy()
        )
        assert info.info.state is JobState.SUCCEEDED

    def test_concurrent_finish_and_cancel_release_once(self) -> None:
        registry = make_registry()
        job = StubJob(registry.new_job_id())
        registry.submit(job, scope=registry.root_scope(), lane=JobLane.FOREGROUND, admission=AdmissionPolicy())
        barrier = threading.Barrier(3)

        def finish() -> None:
            barrier.wait()
            try:
                job._mark_succeeded(exit_code=0)
            except JobStateError:
                # The cancel thread already sealed the terminal state.
                pass

        def cancel() -> None:
            barrier.wait()
            try:
                job.cancel()
                job._mark_cancelled()
            except JobStateError:
                # The finish thread already sealed the terminal state.
                pass

        threads = [threading.Thread(target=finish), threading.Thread(target=cancel)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join()
        assert job.info().state in {JobState.SUCCEEDED, JobState.CANCELLED}
        assert registry.active_count() == 0
        next_job = AutoCompleteJob(registry.new_job_id())
        info = registry.submit(
            next_job, scope=registry.root_scope(), lane=JobLane.FOREGROUND, admission=AdmissionPolicy()
        )
        assert info.info.state is JobState.SUCCEEDED

    def test_start_failure_releases_slot_and_propagates(self) -> None:
        registry = make_registry()
        failing = FailingStartJob(registry.new_job_id())
        with pytest.raises(RuntimeError, match="carrier launch failed"):
            registry.submit(failing, scope=registry.root_scope(), lane=JobLane.FOREGROUND, admission=AdmissionPolicy())
        assert failing.info().state is JobState.FAILED
        assert registry.active_count() == 0
        next_job = AutoCompleteJob(registry.new_job_id())
        info = registry.submit(
            next_job, scope=registry.root_scope(), lane=JobLane.FOREGROUND, admission=AdmissionPolicy()
        )
        assert info.info.state is JobState.SUCCEEDED


class TestScopeClose:
    def test_scope_close_cancels_pending_and_running(self) -> None:
        registry = make_registry()
        user = registry.root_scope().child(JobScopeKind.USER, user_id="u1")
        pending = StubJob(registry.new_job_id())
        registry.register(pending, scope=user, lane=JobLane.FOREGROUND, admission=AdmissionPolicy())
        running = CancellableJob(registry.new_job_id())
        registry.submit(running, scope=user, lane=JobLane.FOREGROUND, admission=AdmissionPolicy())
        report = user.close(timeout=0.5)
        assert pending.info().state is JobState.CANCELLED
        assert running.info().state is JobState.CANCELLED
        assert running._id in report.closed
        assert report.timed_out == ()
        assert report.failed == ()

    def test_sibling_scope_untouched_by_partial_close(self) -> None:
        registry = make_registry()
        user_a = registry.root_scope().child(JobScopeKind.USER, user_id="a")
        user_b = registry.root_scope().child(JobScopeKind.USER, user_id="b")
        job_b = StubJob(registry.new_job_id())
        registry.submit(job_b, scope=user_b, lane=JobLane.FOREGROUND, admission=AdmissionPolicy())
        user_a.close()
        assert job_b.info().state is JobState.RUNNING

    def test_root_close_covers_all_descendants(self) -> None:
        registry = make_registry()
        user_a = registry.root_scope().child(JobScopeKind.USER, user_id="a")
        user_b = registry.root_scope().child(JobScopeKind.USER, user_id="b")
        jobs = [CancellableJob(registry.new_job_id()) for _ in range(3)]
        registry.submit(jobs[0], scope=user_a, lane=JobLane.FOREGROUND, admission=AdmissionPolicy())
        registry.submit(jobs[1], scope=user_b, lane=JobLane.BACKGROUND, admission=AdmissionPolicy())
        registry.register(jobs[2], scope=user_b, lane=JobLane.FOREGROUND, admission=AdmissionPolicy())
        registry.close_all(reason="shutdown", timeout=0.5)
        assert all(job.info().state is JobState.CANCELLED for job in jobs)
        for scope in (registry.root_scope(), user_a, user_b):
            with pytest.raises(JobScopeClosed):
                scope.child(JobScopeKind.TASK)

    def test_close_is_idempotent(self) -> None:
        registry = make_registry()
        user = registry.root_scope().child(JobScopeKind.USER, user_id="u1")
        first = user.close()
        second = user.close()
        assert first.closed == ()
        assert second.closed == ()

    def test_close_report_records_timeouts(self) -> None:
        registry = make_registry()
        user = registry.root_scope().child(JobScopeKind.USER, user_id="u1")
        stuck = StubJob(registry.new_job_id())
        registry.submit(stuck, scope=user, lane=JobLane.FOREGROUND, admission=AdmissionPolicy())
        report = user.close(timeout=0.05)
        assert stuck._id in report.timed_out
        assert report.failed == ()

    def test_close_continues_after_single_failure(self) -> None:
        registry = make_registry()
        user = registry.root_scope().child(JobScopeKind.USER, user_id="u1")

        class ExplodingCancelJob(StubJob):
            def cancel(self, reason: str = "") -> bool:
                raise RuntimeError("cancel exploded")

        broken = ExplodingCancelJob(registry.new_job_id())
        registry.submit(broken, scope=user, lane=JobLane.FOREGROUND, admission=AdmissionPolicy())
        healthy = StubJob(registry.new_job_id())
        thread, _outcomes = start_async(
            registry, healthy, scope=user, lane=JobLane.FOREGROUND, admission=AdmissionPolicy()
        )
        try:
            wait_until(
                lambda: (
                    (info := registry.get(healthy._id)) is not None
                    and info.queued_at is not None
                    and info.admitted_at is None
                )
            )
            report = user.close(timeout=0.05)
            assert broken._id in report.failed
            assert healthy._id in report.closed
            assert healthy.info().state is JobState.CANCELLED
            thread.join(2)
        finally:
            thread.join(2)


class TestHistory:
    def test_history_prunes_oldest_terminal_only(self) -> None:
        registry = JobRegistry(policy=small_policy(), clock=FakeClock(), history_limit=2)
        scope = registry.root_scope()
        completed = [AutoCompleteJob(registry.new_job_id()) for _ in range(3)]
        for job in completed:
            registry.submit(job, scope=scope, lane=JobLane.FOREGROUND, admission=AdmissionPolicy())
        wait_until(lambda: all(job.info().state is JobState.SUCCEEDED for job in completed))
        assert registry.get(completed[0]._id) is None
        assert registry.get(completed[1]._id) is not None
        assert registry.get(completed[2]._id) is not None

    def test_history_never_prunes_active_jobs(self) -> None:
        registry = JobRegistry(policy=small_policy(), clock=FakeClock(), history_limit=2)
        scope = registry.root_scope()
        # The active job lives in a different lane so it never blocks the
        # completed jobs below; history pruning must leave it untouched.
        running = StubJob(registry.new_job_id())
        registry.submit(running, scope=scope, lane=JobLane.SERVICE, admission=AdmissionPolicy())
        completed = [AutoCompleteJob(registry.new_job_id()) for _ in range(4)]
        for job in completed:
            registry.submit(job, scope=scope, lane=JobLane.FOREGROUND, admission=AdmissionPolicy())
        wait_until(lambda: all(job.info().state is JobState.SUCCEEDED for job in completed))
        assert registry.get(running._id) is not None
        assert registry.active_count() == 1

    def test_user_history_limit(self) -> None:
        registry = JobRegistry(policy=small_policy(), clock=FakeClock(), user_history_limit=2)
        user = registry.root_scope().child(JobScopeKind.USER, user_id="u1")
        completed = [AutoCompleteJob(registry.new_job_id()) for _ in range(3)]
        for job in completed:
            registry.submit(job, scope=user, lane=JobLane.FOREGROUND, admission=AdmissionPolicy())
        wait_until(lambda: all(job.info().state is JobState.SUCCEEDED for job in completed))
        assert registry.get(completed[0]._id) is None
        assert registry.get(completed[1]._id) is not None


class TestScopedJobInfo:
    def test_scoped_info_exposes_scheduling_fields(self) -> None:
        registry = make_registry()
        user = registry.root_scope().child(JobScopeKind.USER, user_id="u1")
        job = StubJob(registry.new_job_id())
        info = registry.submit(job, scope=user, lane=JobLane.BACKGROUND, admission=AdmissionPolicy())
        assert info.info.id == job._id
        assert info.lane is JobLane.BACKGROUND
        assert info.scope_id == user.scope_id
        assert info.scope_kind is JobScopeKind.USER
        assert info.parent_job_id is None
        assert info.queued_at is not None
        assert info.admitted_at is not None
        assert info.slot_mode is SlotMode.COUNTED
