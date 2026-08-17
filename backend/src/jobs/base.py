"""Neutral job lifecycle contract shared by registries and carriers.

Defines the job state machine, the immutable ``JobInfo`` snapshot, state
change notifications, and the abstract :class:`Job` base class.  This module
deliberately knows nothing about processes, threads, the runtime, or any
third-party dependency: adapters in later modules implement the carrier
specifics.
"""

from __future__ import annotations

import logging
import threading
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol

from .safety import ClassNameErrorFormatter, ErrorFormatter

logger = logging.getLogger(__name__)

Clock = Callable[[], datetime]
"""Injectable time source; defaults to ``datetime.now(UTC)``."""


class JobState(StrEnum):
    """Lifecycle state of a job. Values are stable wire strings."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class JobKind(StrEnum):
    """Carrier category of a job. Values are stable wire strings."""

    SUBPROCESS = "subprocess"
    THREAD = "thread"
    SERVICE = "service"


TERMINAL_STATES: frozenset[JobState] = frozenset({JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED})


class JobStateError(RuntimeError):
    """An illegal state transition or duplicate start was attempted.

    Raised explicitly instead of silently overwriting job state.
    """


@dataclass(frozen=True, slots=True)
class JobInfo:
    """Immutable snapshot of a job at one point in time.

    ``pids`` is only the snapshot of PIDs the carrier adapter knows about;
    the core model makes no claim about enumerating the full process tree.
    ``error`` only ever holds text produced by an :class:`ErrorFormatter` —
    never a raw exception, command line, environment, or credential content.
    """

    id: str
    kind: JobKind
    state: JobState
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str | None = None
    pids: tuple[int, ...] = ()
    exit_code: int | None = None
    cancel_requested_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class JobStateChange:
    """A domain-neutral notification emitted by a job on lifecycle events.

    ``reason`` is a safe, domain-neutral label (e.g. ``"started"``,
    ``"cancellation_requested"``); internal control details such as cancel
    reasons never leak into it or into ``JobInfo``.
    """

    job_info: JobInfo
    previous_state: JobState
    reason: str


class JobStateListener(Protocol):
    """Receives state change notifications outside the job's lock."""

    def on_job_state_change(self, change: JobStateChange) -> None: ...


class Job(ABC):
    """Abstract lifecycle contract implemented by carrier adapters.

    The base class owns the state machine, timestamps, error formatting, wait
    synchronization, and listener dispatch.  Adapters subclass it, override
    :meth:`start` to launch the carrier (calling ``super().start()`` first),
    report progress through the protected ``_mark_*`` / ``_set_process_info``
    helpers, and implement :meth:`_request_cancel` to deliver their own stop
    signal.
    """

    def __init__(
        self,
        job_id: str,
        kind: JobKind,
        *,
        clock: Clock | None = None,
        error_formatter: ErrorFormatter | None = None,
        listener: JobStateListener | None = None,
    ) -> None:
        self._id = job_id
        self._kind = kind
        self._clock = clock or (lambda: datetime.now(UTC))
        self._error_formatter = error_formatter or ClassNameErrorFormatter()
        self._listeners: set[JobStateListener] = set()
        if listener is not None:
            self._listeners.add(listener)
        self._lock = threading.Lock()
        self._done = threading.Event()
        self._state = JobState.PENDING
        self._started_at: datetime | None = None
        self._finished_at: datetime | None = None
        self._error: str | None = None
        self._pids: tuple[int, ...] = ()
        self._exit_code: int | None = None
        self._cancel_requested_at: datetime | None = None

    # -- public API ---------------------------------------------------------

    def start(self) -> None:
        """Move the job from ``pending`` to ``running``.

        Adapters override this, call ``super().start()``, then launch their
        carrier; a second start raises :class:`JobStateError`.
        """
        self._mark_running()

    def cancel(self, reason: str = "") -> bool:
        """Request cancellation; returns ``True`` if the request was accepted.

        A pending job transitions to ``cancelled`` immediately.  A running job
        records ``cancel_requested_at`` and stays ``running`` — delegating the
        final terminal transition to the adapter via :meth:`_request_cancel` —
        so a job is never marked cancelled while its carrier is still alive.
        Terminal jobs return ``False``.  ``reason`` is internal control
        information and never enters ``JobInfo``.
        """
        with self._lock:
            if self._state is JobState.PENDING:
                self._state = JobState.CANCELLED
                self._finished_at = self._clock()
                self._done.set()
                previous = JobState.PENDING
                notify_reason = "cancelled"
                request_stop = False
            elif self._state is JobState.RUNNING:
                previous = JobState.RUNNING
                notify_reason = "cancellation_requested"
                request_stop = True
                if self._cancel_requested_at is None:
                    self._cancel_requested_at = self._clock()
            else:
                return False
        self._notify(previous, notify_reason)
        if request_stop:
            self._request_cancel()
        return True

    def wait(self, timeout: float | None = None) -> bool:
        """Block until a terminal state or ``timeout``; returns whether a
        terminal state was reached."""
        with self._lock:
            if self._state in TERMINAL_STATES:
                return True
        return self._done.wait(timeout)

    def close(self, timeout: float | None = None) -> None:
        """Cancel and wait. Terminal escalation policy is owned by adapters."""
        if self.info().state not in TERMINAL_STATES:
            self.cancel()
            self.wait(timeout)

    def info(self) -> JobInfo:
        """Return an immutable snapshot of the current job state."""
        with self._lock:
            return JobInfo(
                id=self._id,
                kind=self._kind,
                state=self._state,
                started_at=self._started_at,
                finished_at=self._finished_at,
                error=self._error,
                pids=self._pids,
                exit_code=self._exit_code,
                cancel_requested_at=self._cancel_requested_at,
            )

    def add_listener(self, listener: JobStateListener) -> None:
        """Register a state listener; it receives every change from now on.

        Listeners may be attached before :meth:`start` so a registry never
        misses a fast-completing job.  Dispatch stays outside the job lock.
        """
        with self._lock:
            self._listeners.add(listener)

    def remove_listener(self, listener: JobStateListener) -> None:
        """Stop delivering state changes to a previously added listener."""
        with self._lock:
            self._listeners.discard(listener)

    # -- protected lifecycle helpers for adapters ---------------------------

    def _mark_running(self) -> None:
        """Transition ``pending -> running`` and record ``started_at``."""
        with self._lock:
            self._require(JobState.PENDING, JobState.RUNNING)
            self._state = JobState.RUNNING
            self._started_at = self._clock()
        self._notify(JobState.PENDING, "started")

    def _mark_succeeded(
        self,
        exit_code: int | None = None,
        pids: Iterable[int] = (),
    ) -> None:
        """Transition ``running -> succeeded`` from a ``running`` job only."""
        self._finish(JobState.SUCCEEDED, exit_code=exit_code, pids=pids)

    def _mark_failed(
        self,
        exception: BaseException,
        exit_code: int | None = None,
        pids: Iterable[int] = (),
    ) -> None:
        """Transition ``running -> failed``, persisting only formatted error
        text produced by the injected :class:`ErrorFormatter`."""
        with self._lock:
            self._require(JobState.RUNNING, JobState.FAILED)
            self._state = JobState.FAILED
            self._error = self._error_formatter.format_error(exception)
            self._exit_code = exit_code
            self._pids = tuple(pids)
            self._finished_at = self._clock()
            self._done.set()
        self._notify(JobState.RUNNING, "failed")

    def _mark_cancelled(
        self,
        exit_code: int | None = None,
        pids: Iterable[int] = (),
    ) -> None:
        """Transition ``running -> cancelled`` once the carrier has stopped."""
        self._finish(JobState.CANCELLED, exit_code=exit_code, pids=pids)

    def _set_process_info(
        self,
        pids: Iterable[int],
        exit_code: int | None = None,
    ) -> None:
        """Refresh the known-PID snapshot and optional exit code without
        changing state or notifying listeners."""
        with self._lock:
            self._pids = tuple(pids)
            if exit_code is not None:
                self._exit_code = exit_code

    @abstractmethod
    def _request_cancel(self) -> None:
        """Deliver the carrier-specific stop signal for a running job."""

    # -- internals ----------------------------------------------------------

    def _finish(
        self,
        state: JobState,
        *,
        exit_code: int | None,
        pids: Iterable[int],
    ) -> None:
        with self._lock:
            self._require(JobState.RUNNING, state)
            self._state = state
            self._exit_code = exit_code
            self._pids = tuple(pids)
            self._finished_at = self._clock()
            self._done.set()
        self._notify(JobState.RUNNING, state.value)

    def _require(self, expected: JobState, target: JobState) -> None:
        if self._state is not expected:
            raise JobStateError(f"job {self._id!r} cannot transition from {self._state.value!r} to {target.value!r}")

    def _notify(self, previous: JobState, reason: str) -> None:
        """Dispatch a state change outside the lock; listener failures never
        corrupt job state."""
        with self._lock:
            listeners = tuple(self._listeners)
        if not listeners:
            return
        change = JobStateChange(job_info=self.info(), previous_state=previous, reason=reason)
        for listener in listeners:
            try:
                listener.on_job_state_change(change)
            except Exception:
                logger.exception("job state listener failed for job %r", self._id)
