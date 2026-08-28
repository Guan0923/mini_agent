"""Tests for the cooperative-cancel thread job carrier (``backend.jobs.ThreadJob``).

Uses real short-lived daemon threads and ``threading.Event``/polling for
synchronization so all behaviour is deterministic and fast (no long sleeps).
Registry scenarios run through a real :class:`JobRegistry` to exercise slot
release and the ``timed_out`` close report.
"""

from __future__ import annotations

import logging
import threading
import time

from backend.jobs import AdmissionPolicy, JobLane, JobRegistry, ThreadJob
from backend.jobs.base import JobKind, JobState


def wait_until(predicate, timeout: float = 5.0) -> None:  # noqa: ANN001
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition was not reached in time")


def make_job(
    job_id: str = "job-thread",
    target: callable = lambda: None,  # noqa: ANN001
    *,
    args: tuple = (),
    kwargs: dict | None = None,
    error_formatter=None,
    listener=None,
) -> ThreadJob:
    return ThreadJob(
        job_id,
        target,
        args,
        kwargs,
        error_formatter=error_formatter,
        listener=listener,
    )


# ---------------------------------------------------------------------------
# Normal completion
# ---------------------------------------------------------------------------


def test_normal_completion_succeeds_with_args_kwargs() -> None:
    seen: dict[str, object] = {}

    def target(a, b, *, flag, **extra):  # noqa: ANN001, ANN002, ANN003
        seen["args"] = (a, b)
        seen["flag"] = flag
        seen["extra"] = extra

    job = make_job("job-ok", target, args=(1, 2), kwargs={"flag": True, "mode": "x"})
    job.start()
    assert job.wait(timeout=5) is True
    state_info = job.info()
    assert state_info.state is JobState.SUCCEEDED
    assert state_info.error is None
    assert seen["args"] == (1, 2)
    assert seen["flag"] is True
    assert seen["extra"] == {"mode": "x"}
    assert state_info.kind is JobKind.THREAD


# ---------------------------------------------------------------------------
# Target raises
# ---------------------------------------------------------------------------


def test_target_raises_marks_failed_with_formatted_error() -> None:
    def target() -> None:
        raise ValueError("boom")

    job = make_job("job-fail", target)
    job.start()
    assert job.wait(timeout=5) is True
    info = job.info()
    assert info.state is JobState.FAILED
    assert info.error is not None
    # The default ClassNameErrorFormatter reports only the class name, never
    # the exception message.
    assert info.error == "ValueError"


def test_exception_after_cancel_is_failed_not_cancelled() -> None:
    """A target that raises an unrelated exception after a cancel was requested
    is ``failed`` (formatted error), never ``cancelled``: cancellation wins only
    when the target returns normally."""
    entered = threading.Event()
    raise_now = threading.Event()

    def target() -> None:
        entered.set()
        # Wait for the test to request cancellation, then raise.
        raise_now.wait(10)
        raise ValueError("boom")

    job = make_job("job-raise-after-cancel", target)
    job.start()
    assert entered.wait(5), "target never started"
    assert job.info().state is JobState.RUNNING
    assert job.cancel() is True  # job is provably still running
    raise_now.set()
    assert job.wait(timeout=5) is True
    info = job.info()
    assert info.state is JobState.FAILED
    assert info.cancel_requested_at is not None  # the cancel was still recorded
    assert info.error == "ValueError"  # class name via the default formatter


# ---------------------------------------------------------------------------
# Cooperative cancellation
# ---------------------------------------------------------------------------


def test_cooperative_cancel_target_responds_to_hook() -> None:
    entered = threading.Event()
    finished = threading.Event()

    def target(is_cancelled) -> None:  # noqa: ANN001, ANN002, ANN003
        entered.set()
        while not is_cancelled():
            time.sleep(0.01)
        assert is_cancelled() is True
        finished.set()

    job = make_job("job-cancel-hook", target)
    job.start()
    assert entered.wait(5), "target never started"
    assert job.info().state is JobState.RUNNING
    assert job.cancel() is True
    assert finished.wait(5), "target never observed cancellation"
    assert job.wait(timeout=5) is True
    info = job.info()
    assert info.state is JobState.CANCELLED
    assert info.cancel_requested_at is not None
    assert info.error is None


def test_cancel_event_attribute_is_set() -> None:
    entered = threading.Event()
    release = threading.Event()

    def target() -> None:
        entered.set()
        release.wait(30)

    job = make_job("job-cancel-event", target)
    job.start()
    assert entered.wait(5)
    assert not job.cancel_event.is_set()
    assert job.cancel() is True
    assert job.is_cancelled()
    assert job.cancel_event.is_set()
    release.set()
    assert job.wait(timeout=5) is True
    assert job.info().state is JobState.CANCELLED


def test_cancel_before_seal_still_cancelled_deterministic() -> None:
    """Cancel requested before the worker seals the state wins: final
    state is ``cancelled`` even though the target returns normally."""
    entered = threading.Event()
    proceed = threading.Event()

    def target() -> None:
        entered.set()
        proceed.wait(10)
        # Return normally; the worker checks for a pending cancel next.

    job = make_job("job-cancel-seal", target)
    job.start()
    assert entered.wait(5)
    assert job.cancel() is True
    proceed.set()
    assert job.wait(timeout=5) is True
    info = job.info()
    assert info.state is JobState.CANCELLED
    assert info.cancel_requested_at is not None
    # A later cancel on the terminal job is a no-op.
    assert job.cancel() is False


def test_cancel_after_seal_is_noop_succeeded() -> None:
    """If the worker already sealed ``succeeded`` before cancel(), the cancel
    is rejected and the state stays ``succeeded`` (deterministic)."""

    def target() -> None:
        pass

    job = make_job("job-cancel-late", target)
    job.start()
    assert job.wait(timeout=5) is True
    assert job.info().state is JobState.SUCCEEDED
    assert job.cancel() is False
    assert job.info().state is JobState.SUCCEEDED


# ---------------------------------------------------------------------------
# Ignoring cancel -> close timeout
# ---------------------------------------------------------------------------


def test_close_times_out_logs_diagnostic_and_stays_non_terminal(caplog) -> None:
    entered = threading.Event()
    release = threading.Event()

    def target() -> None:
        entered.set()
        release.wait()

    job = make_job("job-stubborn", target)
    job.start()
    assert entered.wait(5), "target never started"
    with caplog.at_level(logging.WARNING):
        assert job.cancel() is True
        job.close(timeout=0.05)
    assert job.info().state is JobState.RUNNING  # still non-terminal
    # Diagnostic must include the job id and thread name/ident.
    assert any("job-stubborn" in rec.message for rec in caplog.records)
    assert any(
        job._worker_thread is not None and str(job._worker_thread.ident) in rec.message for rec in caplog.records
    )
    assert any(job._worker_thread is not None and job._worker_thread.name in rec.message for rec in caplog.records)
    release.set()
    job.close(timeout=5)
    assert job.info().state is JobState.CANCELLED


def test_scope_close_reports_stubborn_job_in_timed_out() -> None:
    registry = JobRegistry()
    scope = registry.root_scope()
    entered = threading.Event()
    release = threading.Event()

    def target() -> None:
        entered.set()
        release.wait()

    job = ThreadJob(registry.new_job_id(), target)
    registry.submit(job, scope=scope, lane=JobLane.BACKGROUND, admission=AdmissionPolicy())
    assert entered.wait(5), "target never started"
    report = scope.close(timeout=0.05)
    assert job._id in report.timed_out
    assert job._id not in report.closed
    # After the timed-out close the job is still non-terminal.
    assert job.info().state is JobState.RUNNING
    release.set()
    job.close(timeout=5)
    assert job.info().state is JobState.CANCELLED


# ---------------------------------------------------------------------------
# Idempotency / thread hygiene / daemon
# ---------------------------------------------------------------------------


def test_repeated_close_is_safe() -> None:
    def target() -> None:
        pass

    job = make_job("job-close-twice", target)
    job.start()
    assert job.wait(timeout=5) is True
    job.close(timeout=5)
    job.close(timeout=5)
    assert job.info().state is JobState.SUCCEEDED


def test_daemon_attribute_on_worker_thread() -> None:
    entered = threading.Event()
    release = threading.Event()

    def target() -> None:
        entered.set()
        release.wait(30)

    job = make_job("job-daemon", target)
    job.start()
    assert entered.wait(5), "worker never started"
    thread = job._worker_thread
    assert thread is not None
    assert thread.is_alive()
    assert thread.daemon is True
    assert job.info().state is JobState.RUNNING
    release.set()
    assert job.wait(timeout=5) is True


def test_worker_thread_exits_after_terminal() -> None:
    def target() -> None:
        pass

    job = make_job("job-thread-hygiene", target)
    job.start()
    assert job.wait(timeout=5) is True
    thread = job._worker_thread
    assert thread is not None
    thread.join(timeout=5)
    assert not thread.is_alive()


# ---------------------------------------------------------------------------
# Registry integration
# ---------------------------------------------------------------------------


def test_registry_submit_releases_slot_on_terminal() -> None:
    registry = JobRegistry()
    scope = registry.root_scope()
    entered = threading.Event()
    release = threading.Event()

    def target() -> None:
        entered.set()
        release.wait(30)

    job = ThreadJob(registry.new_job_id(), target)
    registry.submit(job, scope=scope, lane=JobLane.FOREGROUND, admission=AdmissionPolicy())
    assert entered.wait(5), "worker never started"
    assert registry.get(job._id) is not None
    snapshot = registry.get(job._id)
    assert snapshot is not None
    assert snapshot.info.state is JobState.RUNNING
    assert snapshot.holds_slot
    assert registry.active_count() == 1
    release.set()
    wait_until(lambda: job.info().state is JobState.SUCCEEDED)
    terminal = registry.get(job._id)
    assert terminal is not None
    assert terminal.info.state is JobState.SUCCEEDED
    assert not terminal.holds_slot
    assert registry.active_count() == 0


def test_registry_submit_cancelled_releases_slot() -> None:
    registry = JobRegistry()
    scope = registry.root_scope()
    entered = threading.Event()
    release = threading.Event()

    def target() -> None:
        entered.set()
        release.wait(30)

    job = ThreadJob(registry.new_job_id(), target)
    registry.submit(job, scope=scope, lane=JobLane.FOREGROUND, admission=AdmissionPolicy())
    assert entered.wait(5)
    assert registry.active_count() == 1
    scope.cancel(job._id)
    release.set()
    wait_until(lambda: job.info().state is JobState.CANCELLED)
    snapshot = registry.get(job._id)
    assert snapshot is not None
    assert not snapshot.holds_slot
    assert registry.active_count() == 0
