"""Resource lanes, limit policies, admission policies, and slot leases.

Lanes partition work by resource class (foreground, background, long-lived
services); limits are applied per system, per user, and per runner scope.
Slot leases are the internal bookkeeping that guarantees a slot is released
exactly once when a job reaches a terminal state.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from .errors import JobAdmissionRejected, JobQueueFull


class JobLane(StrEnum):
    """Resource class of a job. Values are stable wire strings."""

    FOREGROUND = "foreground"
    BACKGROUND = "background"
    SERVICE = "service"


class SlotMode(StrEnum):
    """How a job accounts for lane capacity."""

    COUNTED = "counted"
    INHERIT = "inherit"
    UNMETERED = "unmetered"


class QueueMode(StrEnum):
    """Behaviour when no slot is available at admission time."""

    WAIT = "wait"
    REJECT = "reject"


@dataclass(frozen=True, slots=True)
class LaneLimits:
    """Running and queued caps for one lane at one ownership level."""

    max_running: int
    max_queued: int


@dataclass(frozen=True, slots=True)
class JobLimitPolicy:
    """Hierarchical limits per lane for system, user, and runner levels.

    These are deployment safety limits; ordinary users must never be able to
    raise them through ``runtime_config``.
    """

    system: Mapping[JobLane, LaneLimits]
    user: Mapping[JobLane, LaneLimits]
    runner: Mapping[JobLane, LaneLimits]

    @classmethod
    def defaults(cls) -> JobLimitPolicy:
        system = {
            JobLane.FOREGROUND: LaneLimits(max_running=8, max_queued=256),
            JobLane.BACKGROUND: LaneLimits(max_running=2, max_queued=256),
            JobLane.SERVICE: LaneLimits(max_running=16, max_queued=256),
        }
        user = {
            JobLane.FOREGROUND: LaneLimits(max_running=4, max_queued=32),
            JobLane.BACKGROUND: LaneLimits(max_running=1, max_queued=32),
            JobLane.SERVICE: LaneLimits(max_running=8, max_queued=32),
        }
        runner = {
            JobLane.FOREGROUND: LaneLimits(max_running=4, max_queued=16),
            JobLane.BACKGROUND: LaneLimits(max_running=2, max_queued=16),
            JobLane.SERVICE: LaneLimits(max_running=8, max_queued=16),
        }
        return cls(system=system, user=user, runner=runner)

    def running_limit(self, level: str, lane: JobLane) -> int:
        return getattr(self, level)[lane].max_running

    def queued_limit(self, level: str, lane: JobLane) -> int:
        return getattr(self, level)[lane].max_queued


@dataclass(frozen=True, slots=True)
class AdmissionPolicy:
    """Per-job admission behaviour."""

    queue_mode: QueueMode = QueueMode.WAIT
    queue_timeout_seconds: float | None = 30.0
    slot_mode: SlotMode = SlotMode.COUNTED


class SlotLease:
    """Internal slot lease for one admitted job.

    A lease is released exactly once; callers (the registry) must hold their
    own lock while touching it.
    """

    __slots__ = ("lane", "counted", "released")

    def __init__(self, lane: JobLane, *, counted: bool) -> None:
        self.lane = lane
        self.counted = counted
        self.released = False

    def release(self) -> bool:
        """Mark the lease released; returns whether this call did the release."""
        if self.released:
            return False
        self.released = True
        return True


__all__ = [
    "AdmissionPolicy",
    "JobAdmissionRejected",
    "JobLane",
    "JobLimitPolicy",
    "JobQueueFull",
    "LaneLimits",
    "QueueMode",
    "SlotLease",
    "SlotMode",
]
