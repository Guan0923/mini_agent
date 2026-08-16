"""Unit tests for the neutral job core model (``backend.jobs``).

Covers the state machine contract, cancellation semantics, immutable
``JobInfo`` snapshots, error-safety boundaries, wait semantics, and state
change notifications.
"""

from __future__ import annotations

import threading
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta

import pytest

from backend.jobs import (
    ErrorFormatter,
    Job,
    JobKind,
    JobState,
    JobStateChange,
    JobStateError,
    JobStateListener,
)


class StubJob(Job):
    """A carrier with no real backing process; cancellation is recorded."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.cancel_signals: list[str] = []

    def _request_cancel(self) -> None:
        self.cancel_signals.append("requested")


class FakeClock:
    """Injectable clock returning one-second-stepped timestamps."""

    def __init__(self, start: datetime | None = None) -> None:
        self.now = start or datetime(2025, 1, 1, tzinfo=UTC)

    def __call__(self) -> datetime:
        self.now += timedelta(seconds=1)
        return self.now


class RecordingListener:
    """Records (previous_state, new_state, reason) triples in call order."""

    def __init__(self, *, raise_on: JobState | None = None) -> None:
        self.raise_on = raise_on
        self.changes: list[tuple[JobState, JobState, str]] = []

    def on_job_state_change(self, change: JobStateChange) -> None:
        if change.previous_state is self.raise_on:
            raise RuntimeError("listener failure")
        self.changes.append((change.previous_state, change.job_info.state, change.reason))


def make_job(
    *,
    clock: FakeClock | None = None,
    formatter: ErrorFormatter | None = None,
    listener: JobStateListener | None = None,
) -> StubJob:
    return StubJob(
        "job-1",
        JobKind.THREAD,
        clock=clock or FakeClock(),
        error_formatter=formatter,
        listener=listener,
    )


TERMINAL_STATES = (JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED)


class TestEnumValues:
    def test_job_state_values_are_stable_strings(self) -> None:
        assert JobState.PENDING == "pending"
        assert JobState.RUNNING == "running"
        assert JobState.SUCCEEDED == "succeeded"
        assert JobState.FAILED == "failed"
        assert JobState.CANCELLED == "cancelled"

    def test_job_kind_values_are_stable_strings(self) -> None:
        assert JobKind.SUBPROCESS == "subprocess"
        assert JobKind.THREAD == "thread"
        assert JobKind.SERVICE == "service"


class TestLegalTransitions:
    def test_pending_to_running_to_succeeded(self) -> None:
        job = make_job()
        assert job.info().state is JobState.PENDING
        job.start()
        assert job.info().state is JobState.RUNNING
        job._mark_succeeded(exit_code=0, pids=(10, 11))
        assert job.info().state is JobState.SUCCEEDED

    def test_pending_to_running_to_failed(self) -> None:
        job = make_job()
        job.start()
        job._mark_failed(ValueError("boom"))
        assert job.info().state is JobState.FAILED

    def test_pending_to_running_to_cancelled(self) -> None:
        job = make_job()
        job.start()
        job._mark_cancelled()
        assert job.info().state is JobState.CANCELLED

    def test_pending_cancel_reaches_cancelled_immediately(self) -> None:
        job = make_job()
        assert job.cancel() is True
        assert job.info().state is JobState.CANCELLED
        assert job.info().finished_at is not None
        # A pending job has no carrier, so no stop signal is requested.
        assert job.cancel_signals == []

    def test_running_cancel_records_request_then_adapter_finishes(self) -> None:
        job = make_job()
        job.start()
        assert job.cancel("user asked") is True
        info = job.info()
        assert info.state is JobState.RUNNING
        assert info.cancel_requested_at is not None
        assert job.cancel_signals == ["requested"]
        job._mark_cancelled()
        assert job.info().state is JobState.CANCELLED

    def test_running_job_may_still_succeed_after_cancel_request(self) -> None:
        job = make_job()
        job.start()
        assert job.cancel() is True
        job._mark_succeeded(exit_code=0)
        info = job.info()
        assert info.state is JobState.SUCCEEDED
        assert info.cancel_requested_at is not None

    def test_cancel_returns_false_for_terminal_jobs(self) -> None:
        for terminal in (
            JobState.SUCCEEDED,
            JobState.FAILED,
            JobState.CANCELLED,
        ):
            job = make_job()
            job.start()
            if terminal is JobState.SUCCEEDED:
                job._mark_succeeded()
            elif terminal is JobState.FAILED:
                job._mark_failed(ValueError("boom"))
            else:
                job.cancel()
                job._mark_cancelled()
            assert job.cancel() is False
            assert job.info().state is terminal


class TestIllegalTransitions:
    def test_terminal_markers_rejected_from_pending(self) -> None:
        for marker in (
            lambda job: job._mark_succeeded(),
            lambda job: job._mark_failed(ValueError("boom")),
            lambda job: job._mark_cancelled(),
        ):
            job = make_job()
            with pytest.raises(JobStateError):
                marker(job)
            assert job.info().state is JobState.PENDING

    def test_double_start_rejected(self) -> None:
        job = make_job()
        job.start()
        with pytest.raises(JobStateError):
            job.start()
        assert job.info().state is JobState.RUNNING

    def test_mark_running_rejected_while_running(self) -> None:
        job = make_job()
        job.start()
        with pytest.raises(JobStateError):
            job._mark_running()

    def test_start_rejected_from_every_terminal_state(self) -> None:
        for terminal_state in TERMINAL_STATES:
            job = make_job()
            job.start()
            if terminal_state is JobState.SUCCEEDED:
                job._mark_succeeded()
            elif terminal_state is JobState.FAILED:
                job._mark_failed(ValueError("boom"))
            else:
                job.cancel()
                job._mark_cancelled()
            with pytest.raises(JobStateError):
                job.start()
            assert job.info().state is terminal_state

    def test_all_markers_rejected_from_terminal_states(self) -> None:
        markers = (
            lambda job: job._mark_running(),
            lambda job: job._mark_succeeded(),
            lambda job: job._mark_failed(ValueError("boom")),
            lambda job: job._mark_cancelled(),
        )
        for terminal_state in TERMINAL_STATES:
            for marker in markers:
                job = make_job()
                job.start()
                if terminal_state is JobState.SUCCEEDED:
                    job._mark_succeeded()
                elif terminal_state is JobState.FAILED:
                    job._mark_failed(ValueError("boom"))
                else:
                    job.cancel()
                    job._mark_cancelled()
                with pytest.raises(JobStateError):
                    marker(job)

    def test_illegal_transition_does_not_corrupt_job_info(self) -> None:
        job = make_job()
        job.start()
        job._mark_succeeded(exit_code=0, pids=(1, 2))
        before = job.info()
        with pytest.raises(JobStateError):
            job._mark_failed(ValueError("boom"))
        with pytest.raises(JobStateError):
            job.start()
        with pytest.raises(JobStateError):
            job._mark_cancelled()
        assert job.info() == before


class TestJobInfo:
    def test_timestamps_recorded_from_injected_clock(self) -> None:
        clock = FakeClock(datetime(2025, 6, 1, 12, 0, 0, tzinfo=UTC))
        job = make_job(clock=clock)
        job.start()
        job._mark_succeeded()
        info = job.info()
        assert info.started_at == datetime(2025, 6, 1, 12, 0, 1, tzinfo=UTC)
        assert info.finished_at == datetime(2025, 6, 1, 12, 0, 2, tzinfo=UTC)
        assert info.cancel_requested_at is None

    def test_pids_and_exit_code_recorded(self) -> None:
        job = make_job()
        job.start()
        job._mark_succeeded(exit_code=3, pids=(100, 200))
        info = job.info()
        assert info.pids == (100, 200)
        assert info.exit_code == 3

    def test_set_process_info_updates_pids_and_exit_code(self) -> None:
        job = make_job()
        job.start()
        job._set_process_info((1, 2), exit_code=7)
        info = job.info()
        assert info.pids == (1, 2)
        assert info.exit_code == 7
        job._set_process_info((3,))
        info = job.info()
        assert info.pids == (3,)
        assert info.exit_code == 7

    def test_pids_are_an_immutable_snapshot(self) -> None:
        job = make_job()
        job.start()
        pids = [11, 22]
        job._set_process_info(pids)
        pids.append(33)
        info = job.info()
        assert info.pids == (11, 22)
        assert isinstance(info.pids, tuple)

    def test_job_info_is_immutable(self) -> None:
        job = make_job()
        job.start()
        job._mark_succeeded()
        info = job.info()
        with pytest.raises(FrozenInstanceError):
            info.state = JobState.FAILED  # type: ignore[misc]

    def test_info_is_stable_snapshot_across_mutations(self) -> None:
        job = make_job()
        job.start()
        snapshot = job.info()
        job._mark_succeeded(exit_code=0)
        assert snapshot.state is JobState.RUNNING
        assert snapshot.finished_at is None

    def test_cancel_request_timestamp_recorded(self) -> None:
        clock = FakeClock(datetime(2025, 6, 1, tzinfo=UTC))
        job = make_job(clock=clock)
        job.start()
        job.cancel()
        assert job.info().cancel_requested_at == datetime(2025, 6, 1, 0, 0, 2, tzinfo=UTC)


class TestWait:
    def test_wait_returns_false_before_terminal_with_timeout(self) -> None:
        job = make_job()
        job.start()
        assert job.wait(timeout=0.01) is False

    def test_wait_returns_false_for_pending_job_with_timeout(self) -> None:
        job = make_job()
        assert job.wait(timeout=0.01) is False

    def test_wait_returns_true_once_terminal(self) -> None:
        job = make_job()
        job.start()
        job._mark_succeeded()
        assert job.wait(timeout=0) is True

    def test_wait_blocks_until_terminal_reached(self) -> None:
        job = make_job()
        job.start()

        def finish_later() -> None:
            threading.Event().wait(0.05)
            job._mark_succeeded(exit_code=0)

        thread = threading.Thread(target=finish_later)
        thread.start()
        try:
            assert job.wait(timeout=5) is True
        finally:
            thread.join()
        assert job.info().state is JobState.SUCCEEDED

    def test_wait_returns_false_when_timeout_expires_first(self) -> None:
        job = make_job()
        job.start()
        assert job.wait(timeout=0.01) is False
        assert job.info().state is JobState.RUNNING


class TestClose:
    def test_close_cancels_and_waits(self) -> None:
        job = make_job()
        job.start()
        job.close(timeout=0.05)
        info = job.info()
        assert info.cancel_requested_at is not None
        assert job.cancel_signals == ["requested"]

    def test_close_on_pending_cancels_immediately(self) -> None:
        job = make_job()
        job.close()
        assert job.info().state is JobState.CANCELLED

    def test_close_on_terminal_returns_immediately(self) -> None:
        job = make_job()
        job.start()
        job._mark_succeeded()
        job.close()
        assert job.info().state is JobState.SUCCEEDED


class TestErrorSafety:
    def test_default_formatter_never_leaks_exception_text(self) -> None:
        job = make_job()
        job.start()
        job._mark_failed(ValueError("api_key=super-secret"))
        error = job.info().error
        assert error == "ValueError"
        assert "super-secret" not in (error or "")
        assert "api_key" not in (error or "")

    def test_error_is_always_formatted_text_not_exception(self) -> None:
        job = make_job()
        job.start()
        job._mark_failed(OSError("boom"))
        assert isinstance(job.info().error, str)

    def test_injected_formatter_output_is_written_to_job_info(self) -> None:
        class RecordingFormatter:
            def __init__(self) -> None:
                self.calls: list[BaseException] = []

            def format_error(self, exception: BaseException) -> str:
                self.calls.append(exception)
                return f"redacted:{type(exception).__name__}"

        formatter = RecordingFormatter()
        job = make_job(formatter=formatter)
        job.start()
        job._mark_failed(RuntimeError("sensitive detail"))
        info = job.info()
        assert info.error == "redacted:RuntimeError"
        assert len(formatter.calls) == 1
        assert isinstance(formatter.calls[0], RuntimeError)

    def test_cancel_reason_never_enters_job_info_error(self) -> None:
        job = make_job()
        job.start()
        job.cancel("internal cancellation note")
        info = job.info()
        assert info.error is None
        assert "internal cancellation note" not in repr(info)


class TestNotifications:
    def test_listener_notified_in_transition_order(self) -> None:
        listener = RecordingListener()
        job = make_job(listener=listener)
        job.start()
        job._mark_succeeded()
        assert listener.changes == [
            (JobState.PENDING, JobState.RUNNING, "started"),
            (JobState.RUNNING, JobState.SUCCEEDED, "succeeded"),
        ]

    def test_listener_notified_on_cancel_request_and_final_cancel(self) -> None:
        listener = RecordingListener()
        job = make_job(listener=listener)
        job.start()
        job.cancel()
        job._mark_cancelled()
        assert listener.changes == [
            (JobState.PENDING, JobState.RUNNING, "started"),
            (JobState.RUNNING, JobState.RUNNING, "cancellation_requested"),
            (JobState.RUNNING, JobState.CANCELLED, "cancelled"),
        ]

    def test_listener_notified_when_pending_job_cancelled(self) -> None:
        listener = RecordingListener()
        job = make_job(listener=listener)
        job.cancel()
        assert listener.changes == [(JobState.PENDING, JobState.CANCELLED, "cancelled")]

    def test_listener_notified_on_failure(self) -> None:
        listener = RecordingListener()
        job = make_job(listener=listener)
        job.start()
        job._mark_failed(ValueError("boom"))
        assert listener.changes[-1] == (JobState.RUNNING, JobState.FAILED, "failed")

    def test_listener_exception_does_not_break_job_state(self) -> None:
        failing = RecordingListener(raise_on=JobState.RUNNING)
        healthy = RecordingListener()
        job = make_job(listener=failing)
        job.start()
        job._mark_succeeded()
        assert job.info().state is JobState.SUCCEEDED
        job2 = make_job(listener=healthy)
        job2.start()
        assert healthy.changes == [(JobState.PENDING, JobState.RUNNING, "started")]

    def test_listener_can_read_info_without_deadlock(self) -> None:
        observed: list[JobState] = []

        class ReadingListener:
            def on_job_state_change(self, change: JobStateChange) -> None:
                observed.append(change.job_info.state)
                assert change.job_info is not None

        job = make_job(listener=ReadingListener())
        job.start()
        job._mark_succeeded()
        assert observed == [JobState.RUNNING, JobState.SUCCEEDED]

    def test_multiple_transitions_after_listener_failure_still_notify(self) -> None:
        calls: list[str] = []

        class CountingListener:
            def on_job_state_change(self, change: JobStateChange) -> None:
                calls.append(change.reason)

        job = make_job(listener=CountingListener())
        job.start()
        job.cancel()
        job._mark_cancelled()
        assert calls == ["started", "cancellation_requested", "cancelled"]
