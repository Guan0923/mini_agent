"""Registry query/result contracts and private job record."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from ..base import Job, JobInfo, JobState
from ..scheduling import AdmissionPolicy, JobLane, SlotLease, SlotMode
from ..scope import JobOwner, JobScope, JobScopeKind


@dataclass(frozen=True, slots=True)
class JobQuery:
    """Filter for registry/scope listings."""

    lanes: tuple[JobLane, ...] | None = None
    states: tuple[JobState, ...] | None = None
    session_id: str | None = None


@dataclass(frozen=True, slots=True)
class ScopedJobInfo:
    """Public projection: module 1 ``JobInfo`` plus scheduling metadata.

    Owner identity is intentionally absent; it is internal control
    information used only for quota filtering.
    """

    info: JobInfo
    lane: JobLane
    scope_id: str
    scope_kind: JobScopeKind
    parent_job_id: str | None
    queued_at: datetime | None = None
    admitted_at: datetime | None = None
    slot_mode: SlotMode = SlotMode.COUNTED
    holds_slot: bool = False


@dataclass(frozen=True, slots=True)
class CloseReport:
    """Outcome of a scope close; never carries raw exception text."""

    closed: tuple[str, ...] = ()
    timed_out: tuple[str, ...] = ()
    failed: tuple[str, ...] = ()


class _Record:
    __slots__ = (
        "job",
        "scope",
        "owner",
        "lane",
        "admission",
        "parent_job_id",
        "lease",
        "queued_at",
        "admitted_at",
        "terminal",
    )

    def __init__(
        self,
        job: Job,
        scope: JobScope,
        owner: JobOwner,
        lane: JobLane,
        admission: AdmissionPolicy,
        parent_job_id: str | None,
        queued_at: datetime,
    ) -> None:
        self.job = job
        self.scope = scope
        self.owner = owner
        self.lane = lane
        self.admission = admission
        self.parent_job_id = parent_job_id
        self.lease: SlotLease | None = None
        self.queued_at = queued_at
        self.admitted_at: datetime | None = None
        self.terminal = False
