"""Scope hierarchy, owner inheritance, and access boundaries.

A scope is the in-process ownership and access unit for jobs.  Scopes form a
tree rooted at one system scope per registry; children inherit the parent
owner and may only fill empty owner fields.  Scopes and owners are control
plane information only — they are never persisted into sessions, runtime
state.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from .errors import JobScopeClosed

if TYPE_CHECKING:
    from .registry import CloseReport, JobQuery, JobRegistry, ScopedJobInfo
    from .scheduling import AdmissionPolicy, JobLane


class JobScopeKind(StrEnum):
    """Type of an ownership scope. Values are stable wire strings."""

    SYSTEM = "system"
    SESSION = "session"
    THREAD = "thread"
    RUN = "run"
    TASK = "task"


@dataclass(frozen=True, slots=True)
class JobOwner:
    """Owner identity of a scope; empty fields are inherited from parents."""

    session_id: str | None = None
    thread_id: str | None = None
    run_id: str | None = None


def _merge_owner(
    parent: JobOwner,
    *,
    session_id: str | None,
    thread_id: str | None,
    run_id: str | None,
) -> JobOwner:
    if session_id is not None and parent.session_id not in (None, session_id):
        raise ValueError(f"cannot override inherited session {parent.session_id!r} with {session_id!r}")
    if thread_id is not None and parent.thread_id not in (None, thread_id):
        raise ValueError(f"cannot override inherited thread {parent.thread_id!r} with {thread_id!r}")
    if run_id is not None and parent.run_id not in (None, run_id):
        raise ValueError(f"cannot override inherited run {parent.run_id!r} with {run_id!r}")
    return JobOwner(
        session_id=parent.session_id if session_id is None else session_id,
        thread_id=parent.thread_id if thread_id is None else thread_id,
        run_id=parent.run_id if run_id is None else run_id,
    )


class JobScope:
    """One node of the in-process ownership tree.

    Business callers should prefer the scope-bound API over the root registry:
    ``child/register/start/submit/get/list/cancel/close`` all operate within
    this scope and its descendants.
    """

    def __init__(
        self,
        registry: JobRegistry,
        scope_id: str,
        kind: JobScopeKind,
        parent: JobScope | None,
        parent_job_id: str | None,
        owner: JobOwner,
    ) -> None:
        self._registry = registry
        self._scope_id = scope_id
        self._kind = kind
        self._parent = parent
        self._parent_job_id = parent_job_id
        self._owner = owner

    # -- identity -----------------------------------------------------------

    @property
    def scope_id(self) -> str:
        return self._scope_id

    @property
    def kind(self) -> JobScopeKind:
        return self._kind

    @property
    def parent(self) -> JobScope | None:
        return self._parent

    @property
    def parent_job_id(self) -> str | None:
        return self._parent_job_id

    @property
    def owner(self) -> JobOwner:
        return self._owner

    @property
    def registry(self) -> JobRegistry:
        """Registry that owns this scope, for carrier adapters only."""
        return self._registry

    @property
    def depth(self) -> int:
        depth = 0
        current = self._parent
        while current is not None:
            depth += 1
            current = current.parent
        return depth

    @property
    def closed(self) -> bool:
        return self._registry._is_scope_closed(self)

    # -- scope-bound operations ---------------------------------------------

    def child(
        self,
        kind: JobScopeKind,
        *,
        session_id: str | None = None,
        thread_id: str | None = None,
        run_id: str | None = None,
        parent_job_id: str | None = None,
    ) -> JobScope:
        """Create a child scope inheriting and extending this owner."""
        return self._registry._create_scope(
            self,
            kind,
            session_id=session_id,
            thread_id=thread_id,
            run_id=run_id,
            parent_job_id=parent_job_id,
        )

    def register(self, job: Any, *, lane: JobLane, admission: AdmissionPolicy) -> ScopedJobInfo:
        return self._registry._register_in_scope(self, job, lane=lane, admission=admission)

    def start(self, job_id: str) -> ScopedJobInfo:
        return self._registry._start_in_scope(self, job_id)

    def submit(self, job: Any, *, lane: JobLane, admission: AdmissionPolicy) -> ScopedJobInfo:
        return self._registry._submit_in_scope(self, job, lane=lane, admission=admission)

    def get(self, job_id: str) -> ScopedJobInfo | None:
        return self._registry._get_in_scope(self, job_id)

    def list(self, query: JobQuery | None = None) -> tuple[ScopedJobInfo, ...]:
        return self._registry._list_in_scope(self, query)

    def cancel(self, job_id: str) -> bool:
        return self._registry._cancel_in_scope(self, job_id)

    def close(self, timeout: float | None = None) -> CloseReport:
        """Close this scope and all descendants; the root closes everything."""
        if self._parent is None:
            return self._registry.close_all(reason="root scope closed", timeout=timeout)
        return self._registry._close_scope(self, timeout)


__all__ = ["JobOwner", "JobScope", "JobScopeClosed", "JobScopeKind", "_merge_owner"]
