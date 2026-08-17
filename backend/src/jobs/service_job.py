"""Protocol-agnostic long-lived service adapter (:class:`ServiceJob`).

A :class:`ServiceJob` adapts a long-lived external service to the ``Job``
lifecycle contract through the minimal :class:`ServiceDriver` port.  The job
launches an instance via ``driver.start()``, supervises it with periodic
health probes (``driver.check(handle)``), rebuilds it through fresh
generations when it degrades, and stops the current instance on cancellation
(``driver.stop(handle)``).  It is deliberately neutral: it knows nothing about
MCP, stdio, processes, or environments and depends only on the standard
library and the ``jobs`` package.

**Explicit boundary (do not implement here).**  Third-party MCP stdio
launchers cannot have process control and a minimal environment injected
through this interface: they need POSIX ``exec``-style process control and a
sanitised environment that ``ServiceJob`` deliberately does not provide.  A
later MCP module must implement its own controlled transport
(process-spawning, environment redaction, stderr capture) rather than reuse
``ServiceJob``.  This module implements no MCP protocol.

Lifecycle and ownership model:

* **Health is separate from job state.**  While the job stays ``running`` its
  ``health`` fluctuates between ``healthy`` / ``degraded`` / ``down``.  Health
  transitions are published through the job's state-change channel as reason
  labels (``"service_degraded"``, ``"service_recovered"``) *without* changing
  ``JobInfo.state``.  Because ``JobInfo.state`` stays ``running``, the
  registry's terminal handling (which keys on terminal states only) is never
  triggered by health churn.
* **Degradation.**  ``max_failures`` consecutive probe failures mark the
  service ``degraded`` (publish ``"service_degraded"``) but keep probing the
  same instance; a later successful probe recovers it in place (publish
  ``"service_recovered"``).
* **Rebuild.**  A probe failure *while already degraded* triggers a rebuild:
  the old instance is stopped via ``driver.stop(old_handle)``, a new instance
  is started (a new generation handle), and it is re-initialised.  Every
  generation has an independent shutdown boundary and resource handle; old
  handles are always closed exactly once on rebuild.
* **Rebuild exhaustion.**  After ``max_restarts`` rebuilds without becoming
  healthy the job is marked ``failed``.
* **Cancellation.**  :meth:`_request_cancel` stops the *current* instance.
  :meth:`ServiceJob.close` cancels and waits.  Stops are idempotent and
  thread-safe, so cancelling mid-rebuild never leaks an instance and each
  generation handle is stopped exactly once.

The supervisor runs on a daemon thread (documented daemon boundary): the
process may exit while a slow rebuild lingers, but after a terminal state the
supervisor thread has always exited, so ``close`` joins it and leaves no
dangling thread.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import replace
from enum import StrEnum
from typing import Protocol

from .base import TERMINAL_STATES, Job, JobKind, JobStateError

logger = logging.getLogger(__name__)

__all__ = ["ServiceDriver", "ServiceHealth", "ServiceJob"]


class ServiceStartupError(Exception):
    """The service failed to become healthy within the initialisation timeout."""


class ServiceRebuildError(Exception):
    """The service consumed its rebuild budget without recovering health."""


class ServiceDriver(Protocol):
    """Minimal controlled interface a long-lived service exposes to the job.

    A concrete driver is constructed and injected by the caller (tests use a
    fake).  Handles are opaque per-instance identity objects; each
    :meth:`start` must return a fresh handle, and :meth:`stop` must release the
    resources owned by that exact instance.
    """

    def start(self) -> object:
        """Start one service instance and return its resource handle."""
        ...

    def check(self, handle: object) -> bool:
        """Probe the instance; return ``True`` when it is healthy."""
        ...

    def stop(self, handle: object) -> None:
        """Stop the instance and release its resource handle."""
        ...


class ServiceHealth(StrEnum):
    """Health of the supervised service, distinct from the job's lifecycle.

    Values are stable wire strings.  Transitions into/out of ``degraded`` are
    published as ``"service_degraded"`` / ``"service_recovered"``; ``down`` is
    the transient value while no live healthy instance exists and is not
    published.
    """

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    DOWN = "down"


class ServiceJob(Job):
    """Adapt a long-lived service to the :class:`Job` lifecycle contract.

    Args:
        job_id: Stable identifier for the job.
        driver: The actual service driver; constructed and injected by the
            caller.
        init_timeout_seconds: Deadline within which a freshly started instance
            must first become healthy, or startup/rebuild is treated as failed.
        check_interval_seconds: Delay between health probes.
        max_failures: Consecutive probe failures before the service is marked
            ``degraded``.
        max_restarts: Number of rebuilds allowed before the job is finally
            marked ``failed``.
        error_formatter, clock, listener: Forwarded to the :class:`Job` base
            class.
    """

    kind = JobKind.SERVICE

    def __init__(
        self,
        job_id: str,
        driver: ServiceDriver,
        *,
        init_timeout_seconds: float = 30.0,
        check_interval_seconds: float = 1.0,
        max_failures: int = 3,
        max_restarts: int = 5,
        error_formatter=None,
        clock=None,
        listener=None,
    ) -> None:
        super().__init__(
            job_id,
            self.kind,
            clock=clock,
            error_formatter=error_formatter,
            listener=listener,
        )
        self._driver = driver
        self._init_timeout_seconds = init_timeout_seconds
        self._check_interval_seconds = check_interval_seconds
        self._max_failures = max_failures
        self._max_restarts = max_restarts
        self._supervisor_thread: threading.Thread | None = None
        self._active_handle: object | None = None
        self._stopped_handles: set[object] = set()
        self._health = ServiceHealth.DOWN
        self._rebuild_count = 0
        self._force_rebuild = threading.Event()
        self._external_failures = 0

    # -- public API ---------------------------------------------------------

    @property
    def health(self) -> ServiceHealth:
        """Current service health; independent of :meth:`Job.info` state."""
        with self._lock:
            return self._health

    def info(self):
        """Include service health without changing the core state machine."""
        return replace(super().info(), health=self.health.value)

    def report_failure(self) -> None:
        """Report a failed service call to the supervisor.

        Transport adapters can call this when a request times out or returns
        an exception.  The supervisor remains the only component that stops
        and rebuilds the live instance.
        """
        with self._lock:
            self._external_failures += 1
            failures = self._external_failures
        if failures >= self._max_failures:
            self._set_health(ServiceHealth.DEGRADED)
        if failures > self._max_failures:
            self._force_rebuild.set()

    def report_success(self) -> None:
        """Reset call-failure streak and recover a degraded service."""
        with self._lock:
            self._external_failures = 0
        if self.health is ServiceHealth.DEGRADED:
            self._set_health(ServiceHealth.HEALTHY)

    def start(self) -> None:
        """Transition to ``running`` and launch the daemon supervisor thread.

        Calls ``super().start()`` first; a second ``start`` raises
        :class:`~backend.jobs.JobStateError`.  The driver's first instance is
        started by the supervisor thread, not here.
        """
        super().start()
        self._supervisor_thread = threading.Thread(
            target=self._run,
            name=f"job-{self._id}-supervisor",
            daemon=True,
        )
        self._supervisor_thread.start()

    def close(self, timeout: float | None = None) -> None:
        """Cancel, wait for the supervisor, and join it (idempotent).

        After a terminal state the supervisor thread has already exited and
        joining leaves no dangling handle.  A blocking ``driver.check`` /
        ``driver.stop`` that ignores cancellation can keep the supervisor alive
        past the join window; that is logged as a warning naming the job and
        thread (mirroring :class:`~backend.jobs.ThreadJob`) and the job stays
        non-terminal so a later ``close`` after the driver unblocks still
        finishes cleanly.
        """
        thread = self._supervisor_thread
        if self.info().state not in TERMINAL_STATES:
            super().close(timeout)
        if thread is not None:
            thread.join(timeout=5.0)
            if thread.is_alive():
                logger.warning(
                    "job %s supervisor thread %s (ident %s) did not finish within the close timeout; cancel requested: %s",
                    self._id,
                    thread.name,
                    thread.ident,
                    self._cancelled(),
                )

    # -- cancellation -------------------------------------------------------

    def _request_cancel(self) -> None:
        """Stop the current instance (idempotent, thread-safe).

        Uses :meth:`_stop_handle` so a cancellation arriving during a rebuild
        can never leak an instance: whichever generation handle is current is
        closed exactly once, and handles already closed are skipped.
        """
        with self._lock:
            handle = self._active_handle
        if handle is not None:
            self._stop_handle(handle)

    # -- supervisor thread --------------------------------------------------

    def _run(self) -> None:
        try:
            self._serve()
        except JobStateError:
            # A concurrent cancel/close already sealed the terminal state.
            pass
        except BaseException as exc:
            # A driver failure surfaced off the supervision path.
            try:
                self._mark_failed(exc)
            except JobStateError:
                pass

    def _serve(self) -> None:
        handle = self._start_instance()
        if handle is None:  # cancelled during the very first start
            self._mark_cancelled()
            return
        if not self._initialize(handle):
            if self._cancelled():
                self._mark_cancelled()
                return
            self._stop_handle(self._active_handle)
            self._set_health(ServiceHealth.DOWN)
            self._mark_failed(ServiceStartupError("service failed to become healthy during initialisation"))
            return
        self._set_health(ServiceHealth.HEALTHY)

        consecutive_failures = 0
        while not self._cancelled():
            if self._force_rebuild.is_set():
                self._force_rebuild.clear()
                consecutive_failures = self._max_failures + 1
            if self._check_current():
                consecutive_failures = 0
                if self.health is ServiceHealth.DEGRADED:
                    self._set_health(ServiceHealth.HEALTHY)  # recovered
                continue
            consecutive_failures += 1
            if consecutive_failures < self._max_failures:
                continue
            if consecutive_failures == self._max_failures:
                self._set_health(ServiceHealth.DEGRADED)
                continue
            # A failure while already degraded triggers a rebuild.
            self._stop_handle(self._active_handle)
            if self._rebuild_count >= self._max_restarts:
                self._set_health(ServiceHealth.DOWN)
                self._mark_failed(ServiceRebuildError("service exhausted its rebuild budget without recovering health"))
                return
            self._rebuild_count += 1
            consecutive_failures = 0
            new_handle = self._start_instance()
            if new_handle is None:  # cancelled during the rebuild start
                self._mark_cancelled()
                return
            if self._initialize(new_handle):
                self._set_health(ServiceHealth.HEALTHY)
            # else stay degraded; the next failure streak triggers a rebuild.

        # Clean exit on cancellation: shut the current instance down.
        self._set_health(ServiceHealth.DOWN)
        self._mark_cancelled()

    def _start_instance(self) -> object | None:
        handle = self._driver.start()
        with self._lock:
            self._active_handle = handle
        if self._cancelled():
            self._stop_handle(handle)
            with self._lock:
                self._active_handle = None
            return None
        return handle

    def _initialize(self, handle: object) -> bool:
        deadline = time.monotonic() + self._init_timeout_seconds
        while time.monotonic() < deadline:
            if self._cancelled():
                return False
            if self._probe(handle):
                return True
            time.sleep(self._check_interval_seconds)
        return False

    def _check_current(self) -> bool:
        with self._lock:
            handle = self._active_handle
        if handle is None:
            return False
        return self._probe(handle)

    def _probe(self, handle: object) -> bool:
        try:
            return bool(self._driver.check(handle))
        except Exception:
            logger.exception("service health probe failed for job %r", self._id)
            return False

    def _cancelled(self) -> bool:
        return self.info().cancel_requested_at is not None

    # -- health + idempotent shutdown ---------------------------------------

    def _set_health(self, new_health: ServiceHealth) -> None:
        with self._lock:
            if new_health is self._health:
                return
            old_health = self._health
            self._health = new_health
            if old_health is ServiceHealth.HEALTHY and new_health is ServiceHealth.DEGRADED:
                reason = "service_degraded"
            elif old_health is ServiceHealth.DEGRADED and new_health is ServiceHealth.HEALTHY:
                reason = "service_recovered"
            else:
                reason = None
        if reason is not None:
            self._notify(self.info().state, reason)

    def _stop_handle(self, handle: object) -> None:
        """Stop a handle exactly once, guarded against concurrent stops."""
        with self._lock:
            if handle in self._stopped_handles:
                return
            self._stopped_handles.add(handle)
        try:
            self._driver.stop(handle)
        except Exception:
            logger.exception("service stop failed for job %r handle %r", self._id, handle)
