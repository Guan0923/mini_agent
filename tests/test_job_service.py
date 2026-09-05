"""Tests for the long-lived service job adapter (``backend.jobs.ServiceJob``).

Every test drives a deterministic, inspectable fake :class:`ServiceDriver`
that scripts per-generation probe outcomes (each ``driver.start()`` yields a
fresh handle generation), records every start/stop/check call, and optionally
blocks a handle's probes behind an event so a rebuild can be paused mid-flight.
The fake makes the supervisor's health machine fully deterministic with no
real services or sleeps beyond the tiny configured check interval.
"""

from __future__ import annotations

import itertools
import logging
import threading
import time

from backend.jobs import AdmissionPolicy, JobLane, JobRegistry
from backend.jobs.base import JobKind, JobState
from backend.jobs.service_job import ServiceHealth, ServiceJob


def wait_until(predicate, timeout: float = 5.0) -> None:  # noqa: ANN001
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("condition was not reached in time")


class RecordingListener:
    """Records every state-change notification (previous state, state, reason)."""

    def __init__(self) -> None:
        self.reasons: list[str] = []
        self.states: list[JobState] = []

    def on_job_state_change(self, change) -> None:  # noqa: ANN001
        self.reasons.append(change.reason)
        self.states.append(change.job_info.state)


class FakeDriver:
    """Scripted, inspectable service driver.

    Each :meth:`start` returns a new handle of generation ``g`` (``"h<g>"``).
    ``plans[g]`` is a list of per-call outcomes; when exhausted, ``check``
    returns ``default``.  ``block_check[handle]`` makes that handle's probes
    block until the event is set (used to pause a rebuild or keep the
    supervisor stuck), signalling each entry via ``entered_block[handle]``.
    """

    def __init__(self, *, plans: dict[int, list[bool]] | None = None, default: bool = True) -> None:
        self.plans = plans or {}
        self.default = default
        self.started: list[str] = []
        self.stopped: list[str] = []
        self.checks: list[tuple[str, bool]] = []
        self.block_check: dict[str, threading.Event] = {}
        self.entered_block: dict[str, threading.Event] = {}
        self._gen = itertools.count(1)

    def start(self) -> str:
        handle = f"h{next(self._gen)}"
        self.started.append(handle)
        return handle

    def check(self, handle: str) -> bool:
        if handle in self.block_check:
            self.entered_block.setdefault(handle, threading.Event()).set()
            self.block_check[handle].wait(30)
        gen = int(handle[1:])
        plan = self.plans.get(gen)
        if plan:
            outcome = plan[0]
            del plan[0]
        else:
            outcome = self.default
        self.checks.append((handle, outcome))
        return outcome

    def stop(self, handle: str) -> None:
        self.stopped.append(handle)


def make_job(
    driver: FakeDriver,
    job_id: str = "job-svc-1",
    *,
    init_timeout_seconds: float = 1.0,
    check_interval_seconds: float = 0.01,
    max_failures: int = 2,
    max_restarts: int = 2,
    listener: RecordingListener | None = None,
) -> ServiceJob:
    return ServiceJob(
        job_id,
        driver,
        init_timeout_seconds=init_timeout_seconds,
        check_interval_seconds=check_interval_seconds,
        max_failures=max_failures,
        max_restarts=max_restarts,
        listener=listener,
    )


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------


def test_startup_failure_when_probe_never_passes_within_init_timeout() -> None:
    driver = FakeDriver(plans={1: [False]}, default=False)
    job = make_job(driver, init_timeout_seconds=0.05)
    job.start()
    assert job.wait(timeout=5) is True
    info = job.info()
    assert info.state is JobState.FAILED
    assert info.error == "service failed to become healthy during initialisation"
    assert info.kind is JobKind.SERVICE
    # The initial instance was stopped once; no leak.
    assert driver.stopped == ["h1"]


def test_normal_start_reaches_healthy_with_driver_handle() -> None:
    driver = FakeDriver(plans={1: [True]}, default=True)
    job = make_job(driver)
    job.start()
    wait_until(lambda: job.health is ServiceHealth.HEALTHY)
    assert job.info().state is JobState.RUNNING
    assert job.health is ServiceHealth.HEALTHY
    assert job.info().started_at is not None
    assert driver.started == ["h1"]
    assert driver.stopped == []
    job.close(timeout=5)


def test_healthy_probes_observe_interval_and_cancel_wakes_waiter() -> None:
    checked = threading.Event()
    times: list[float] = []

    class TimedDriver(FakeDriver):
        def check(self, handle: str) -> bool:
            times.append(time.monotonic())
            if len(times) == 2:
                checked.set()
            return super().check(handle)

    job = make_job(TimedDriver(), check_interval_seconds=0.1)
    job.start()
    try:
        assert checked.wait(5)
        assert times[1] - times[0] >= 0.09
        job._check_interval_seconds = 60
    finally:
        job.close(timeout=1)
    assert job.info().state is JobState.CANCELLED


# ---------------------------------------------------------------------------
# Probe degradation / recovery on the same instance
# ---------------------------------------------------------------------------


def test_probe_failure_streak_degrades_then_recovers_health_only() -> None:
    listener = RecordingListener()
    # h1: healthy (init), healthy (steady), then 2 failures reach max_failures,
    # then a success recovers the SAME instance before any rebuild threshold.
    driver = FakeDriver(plans={1: [True, True, False, False, True]}, default=True)
    job = make_job(driver, max_failures=2, listener=listener)
    job.start()
    wait_until(lambda: "service_degraded" in listener.reasons)
    wait_until(lambda: "service_recovered" in listener.reasons)
    # Main lifecycle state never left RUNNING despite the health churn.
    assert job.info().state is JobState.RUNNING
    assert job.health is ServiceHealth.HEALTHY
    # No rebuild happened: one instance, nothing stopped.
    assert driver.started == ["h1"]
    assert driver.stopped == []
    # reason sequence observed through the state-change channel.
    assert listener.reasons == ["started", "service_degraded", "service_recovered"]
    job.close(timeout=5)


def test_health_changes_never_alter_job_main_state() -> None:
    listener = RecordingListener()
    driver = FakeDriver(plans={1: [True, False, False, True]}, default=True)
    job = make_job(driver, max_failures=2, listener=listener)
    job.start()
    wait_until(lambda: "service_degraded" in listener.reasons)
    wait_until(lambda: job.health is ServiceHealth.HEALTHY)
    # Every notification carried RUNNING as its job state.
    assert all(state is JobState.RUNNING for state in listener.states)
    job.close(timeout=5)


# ---------------------------------------------------------------------------
# Rebuild (new generation replaces the old instance)
# ---------------------------------------------------------------------------


def test_consecutive_failures_rebuild_stops_old_and_starts_new() -> None:
    listener = RecordingListener()
    # h1 healthy, then 3 failures: 2 reach max_failures -> degraded, the 3rd
    # occurs while already degraded -> rebuild. h2 initializes healthy.
    driver = FakeDriver(plans={1: [True, False, False, False], 2: [True]}, default=True)
    job = make_job(driver, max_failures=2, max_restarts=2, listener=listener)
    job.start()
    wait_until(lambda: job._active_handle == "h2" and job.health is ServiceHealth.HEALTHY)
    assert driver.started == ["h1", "h2"]
    # The old generation handle was closed exactly once on rebuild.
    assert driver.stopped == ["h1"]
    assert job.info().state is JobState.RUNNING
    assert listener.reasons[-2:] == ["service_degraded", "service_recovered"]
    job.close(timeout=5)


# ---------------------------------------------------------------------------
# Rebuild exhaustion
# ---------------------------------------------------------------------------


def test_rebuild_exhaustion_marks_failed() -> None:
    driver = FakeDriver(plans={1: [True, False], 2: [False], 3: [False]}, default=False)
    job = make_job(driver, init_timeout_seconds=0.05, max_failures=2, max_restarts=2)
    job.start()
    assert job.wait(timeout=5) is True
    info = job.info()
    assert info.state is JobState.FAILED
    assert info.error == "service exhausted its rebuild budget without recovering health"
    # Initial instance plus max_restarts rebuilds, each stopped exactly once.
    assert driver.started == ["h1", "h2", "h3"]
    assert driver.stopped == ["h1", "h2", "h3"]
    assert len(driver.stopped) == len(set(driver.stopped))


# ---------------------------------------------------------------------------
# Cancellation / close
# ---------------------------------------------------------------------------


def test_cancel_stops_current_instance_and_seals_cancelled() -> None:
    driver = FakeDriver(plans={1: [True]}, default=True)
    job = make_job(driver)
    job.start()
    wait_until(lambda: job.health is ServiceHealth.HEALTHY)
    assert job.cancel() is True
    assert job.wait(timeout=5) is True
    assert job.info().state is JobState.CANCELLED
    assert job.info().cancel_requested_at is not None
    assert driver.stopped == ["h1"]


def test_close_is_idempotent_and_safe() -> None:
    driver = FakeDriver(plans={1: [True]}, default=True)
    job = make_job(driver)
    job.start()
    wait_until(lambda: job.health is ServiceHealth.HEALTHY)
    job.close(timeout=5)
    job.close(timeout=5)
    assert job.info().state is JobState.CANCELLED
    # Current instance stopped exactly once despite repeated close.
    assert driver.stopped == ["h1"]


def test_cancel_during_rebuild_stops_each_generation_exactly_once() -> None:
    # h1 healthy, then 3 failures (2 -> degraded, the 3rd -> rebuild), then h2
    # starts initialising and its first probe blocks on the gate.
    driver = FakeDriver(plans={1: [True, False, False, False], 2: [True]}, default=True)
    entered_rebuild = threading.Event()
    gate = threading.Event()
    driver.block_check["h2"] = gate
    job = make_job(driver, max_failures=2, max_restarts=2)
    job.start()
    # Wait until the supervisor has stopped h1 and started initializing h2.
    wait_until(lambda: driver.started == ["h1", "h2"])
    entered_rebuild.set()
    job.cancel()
    gate.set()
    entered_rebuild.wait(5)
    assert job.wait(timeout=5) is True
    assert job.info().state is JobState.CANCELLED
    # h1 was closed on the rebuild; h2 was closed by the cancellation.
    assert driver.stopped == ["h1", "h2"]
    assert len(driver.stopped) == len(set(driver.stopped))


# ---------------------------------------------------------------------------
# Registry integration
# ---------------------------------------------------------------------------


def test_scope_close_stops_a_registered_service_job() -> None:
    driver = FakeDriver(plans={1: [True]}, default=True)
    registry = JobRegistry()
    scope = registry.root_scope()
    job = make_job(driver, job_id=registry.new_job_id())
    info = registry.submit(job, scope=scope, lane=JobLane.FOREGROUND, admission=AdmissionPolicy())
    assert info.info.state is JobState.RUNNING
    assert info.holds_slot
    wait_until(lambda: job.health is ServiceHealth.HEALTHY)
    report = scope.close(timeout=5)
    # The scope close cancels and stops the service instance.
    assert job.info().state is JobState.CANCELLED
    assert job._id in report.closed
    assert driver.stopped == ["h1"]
    snapshot = registry.get(job._id)
    assert snapshot is not None and not snapshot.holds_slot


# ---------------------------------------------------------------------------
# Thread hygiene
# ---------------------------------------------------------------------------


def test_supervisor_thread_exits_after_terminal_state() -> None:
    driver = FakeDriver(plans={1: [True]}, default=True)
    job = make_job(driver)
    job.start()
    wait_until(lambda: job.health is ServiceHealth.HEALTHY)
    job.cancel()
    assert job.wait(timeout=5) is True
    thread = job._supervisor_thread
    assert thread is not None
    thread.join(timeout=5)
    assert not thread.is_alive()


def test_close_logs_warning_when_supervisor_thread_does_not_exit(caplog) -> None:
    """A blocking driver.check that ignores cancellation must be logged, not
    silently dropped, and the job stays non-terminal until it finally exits."""
    driver = FakeDriver(plans={1: [True]}, default=True)
    job = make_job(driver)
    job.start()
    wait_until(lambda: job.health is ServiceHealth.HEALTHY)

    # Make the next probe of h1 block; the supervisor is then stuck in check.
    gate = threading.Event()
    driver.block_check["h1"] = gate
    wait_until(lambda: "h1" in driver.entered_block)
    assert driver.entered_block["h1"].wait(5), "supervisor never blocked on a probe"
    caplog.set_level(logging.WARNING, logger="backend.jobs.service_job")

    close_thread = threading.Thread(target=lambda: job.close(timeout=0.2))
    close_thread.start()

    def warning_emitted() -> bool:
        return any(
            rec.levelno == logging.WARNING
            and rec.name == "backend.jobs.service_job"
            and "did not finish" in rec.message
            and job._id in rec.message
            for rec in caplog.records
        )

    wait_until(warning_emitted, timeout=10)
    thread = job._supervisor_thread
    assert thread is not None
    # The warning names the thread (and the job); the supervisor is still stuck.
    assert any(thread.name in rec.message for rec in caplog.records)
    assert any(rec.levelno == logging.WARNING and str(thread.ident) in rec.message for rec in caplog.records)
    # Job is documented non-terminal while the supervisor cannot exit.
    assert job.info().state is JobState.RUNNING

    gate.set()  # let the supervisor finally finish
    wait_until(lambda: job.info().state is JobState.CANCELLED)
    close_thread.join(timeout=10)
    thread = job._supervisor_thread
    assert thread is not None and not thread.is_alive()
    # close is idempotent afterwards.
    job.close(timeout=2)
    assert job.info().state is JobState.CANCELLED
