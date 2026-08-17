"""Cooperative-cancel thread job carrier (:class:`ThreadJob`).

A :class:`ThreadJob` runs a Python callable on a daemon thread started by
:meth:`~ThreadJob.start` and drives it to a terminal lifecycle state from that
same thread: a normal return becomes ``succeeded``, a raised exception becomes
``failed`` (formatted via the injected :class:`ErrorFormatter`), and a return
observed after a cancellation request becomes ``cancelled``.

Cancellation is strictly *cooperative*: the job never tries to force-kill the
Python thread.  It holds a :class:`threading.Event` that :meth:`subclass
cancellation <Job.cancel>` sets through :meth:`_request_cancel`, and the target
exits early by polling it.  Two minimal cooperative hooks are exposed:

* the public ``cancel_event`` attribute (a ``threading.Event``) the target can
  poll directly, and
* a callable ``is_cancelled: Callable[[], bool]`` injected into the target's
  keyword arguments if (and only if) the target declares such a parameter; the
  target can then accept an ``is_cancelled`` keyword argument and return early
  when it reports ``True``.

If a target ignores cancellation and keeps running, :meth:`ThreadJob.close`
waits up to its timeout and then logs a diagnostic (job id and thread
name/ident) stating the thread did not finish in time.  The job stays
non-terminal so a later ``close``/``cancel`` still works, and because the
thread is ``daemon=True`` the process may still exit while the thread lingers.
"""

from __future__ import annotations

import inspect
import logging
import threading
from collections.abc import Callable

from .base import TERMINAL_STATES, Job, JobKind, JobStateError

logger = logging.getLogger(__name__)

__all__ = ["ThreadJob"]


class ThreadJob(Job):
    """One callable executed on a daemon thread with cooperative cancellation.

    Args:
        job_id: Stable identifier for the job.
        target: Callable to run as ``target(*args, **kwargs)`` on the daemon
            thread.  If it declares an ``is_cancelled`` keyword-only parameter,
            the job injects a ``Callable[[], bool]`` that reports whether a
            cancellation has been requested.
        args: Positional arguments forwarded to ``target``.
        kwargs: Keyword arguments forwarded to ``target``.
        error_formatter: ``ErrorFormatter`` for ``JobInfo.error``; defaults to
            :class:`~backend.jobs.ClassNameErrorFormatter` (class name only).
        clock, listener: Injectable time source and state listener, forwarded
            to the :class:`Job` base class.
    """

    kind = JobKind.THREAD

    def __init__(
        self,
        job_id: str,
        target: Callable[..., object],
        args: tuple[object, ...] = (),
        kwargs: dict[str, object] | None = None,
        *,
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
        self._target = target
        self._args = args
        self._kwargs = kwargs.copy() if kwargs is not None else {}
        self._worker_thread: threading.Thread | None = None
        self.cancel_event = threading.Event()

    # -- public API ---------------------------------------------------------

    def start(self) -> None:
        """Transition to ``running`` and launch the daemon worker thread.

        Calls ``super().start()`` first.  A second ``start`` raises
        :class:`~backend.jobs.JobStateError`.
        """
        super().start()
        self._worker_thread = threading.Thread(
            target=self._run,
            name=f"job-{self._id}-worker",
            daemon=True,
        )
        self._worker_thread.start()

    def close(self, timeout: float | None = None) -> None:
        """Cancel, wait for the worker, and report uncooperative threads.

        If the worker ignores cancellation and is still alive when ``timeout``
        expires, a diagnostic is logged (job id and thread name/ident) and the
        job stays non-terminal so a later ``close``/``cancel`` still works.
        Idempotent: calling ``close`` again is safe, and after a terminal state
        the worker thread is joined so no dangling handle is left behind.
        """
        thread = self._worker_thread
        if self.info().state not in TERMINAL_STATES:
            super().close(timeout)
            if thread is not None and thread.is_alive():
                logger.warning(
                    "job %s thread %s (ident %s) did not finish within the close timeout; cancel requested: %s",
                    self._id,
                    thread.name,
                    thread.ident,
                    self.info().cancel_requested_at is not None,
                )
            return
        # Terminal already; join so the thread handle never dangles.
        if thread is not None:
            thread.join(timeout=5.0)

    # -- cooperative cancellation -------------------------------------------

    def is_cancelled(self) -> bool:
        """Report whether a cancellation has been requested (the injected hook)."""
        return self.cancel_event.is_set()

    def _request_cancel(self) -> None:
        """Signal the cooperative cancel event; never kills the thread.

        Called by the base :meth:`Job.cancel` for a running job.  The target
        observes it through ``cancel_event`` / the injected ``is_cancelled``
        callable and returns early; the worker then seals the ``cancelled``
        state.
        """
        self.cancel_event.set()

    # -- worker thread ------------------------------------------------------

    def _run(self) -> None:
        kwargs = self._kwargs
        if _target_accepts_cancel_hook(self._target) and "is_cancelled" not in kwargs:
            kwargs = {**kwargs, "is_cancelled": self.is_cancelled}
        try:
            try:
                self._target(*self._args, **kwargs)
                cancelled = self.info().cancel_requested_at is not None
                if cancelled:
                    self._mark_cancelled()
                else:
                    self._mark_succeeded()
            except BaseException as exc:
                if self.info().cancel_requested_at is not None:
                    # A concurrent cancel sealed first; never surface a failure
                    # for a job that was asked to stop.
                    self._mark_cancelled()
                else:
                    self._mark_failed(exc)
        except JobStateError:
            # A concurrent cancel/close already sealed the terminal state.
            pass


def _target_accepts_cancel_hook(target: Callable[..., object]) -> bool:
    """Whether ``target`` declares an ``is_cancelled`` parameter (the hook)."""
    try:
        parameters = inspect.signature(target).parameters
    except (TypeError, ValueError):
        return False
    return "is_cancelled" in parameters
