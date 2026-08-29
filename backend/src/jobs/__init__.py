"""Neutral job lifecycle contract: stable public types and the Job base class.

Importing ``backend.jobs`` loads no runtime, tools, MCP, API, storage, or
third-party modules; later modules build registries, carriers, and the
runtime integration on top of these primitives without reimplementing the
state machine or the scheduling rules.
"""

from .base import (
    TERMINAL_STATES,
    Clock,
    Job,
    JobInfo,
    JobKind,
    JobState,
    JobStateChange,
    JobStateError,
    JobStateListener,
)
from .errors import (
    JobAdmissionRejected,
    JobAdmissionTimeout,
    JobCloseTimeout,
    JobNotFound,
    JobQueueFull,
    JobRegistrationError,
    JobScopeClosed,
)
from .output import CommandError, MessageErrorFormatter, format_command_output
from .process_group import ProcessFactory, ProcessGroup, TreeTerminator
from .registry import CloseReport, JobQuery, JobRegistry, ScopedJobInfo
from .safety import ClassNameErrorFormatter, ErrorFormatter
from .scheduling import (
    AdmissionPolicy,
    JobLane,
    JobLimitPolicy,
    LaneLimits,
    QueueMode,
    SlotLease,
    SlotMode,
)
from .scope import JobOwner, JobScope, JobScopeKind
from .service_job import ServiceDriver, ServiceHealth, ServiceJob
from .subprocess_job import SubprocessJob
from .thread_job import ThreadJob

__all__ = [
    "AdmissionPolicy",
    "ClassNameErrorFormatter",
    "Clock",
    "CloseReport",
    "CommandError",
    "ErrorFormatter",
    "format_command_output",
    "Job",
    "JobAdmissionRejected",
    "JobAdmissionTimeout",
    "JobCloseTimeout",
    "JobInfo",
    "JobKind",
    "JobLane",
    "JobLimitPolicy",
    "JobNotFound",
    "JobOwner",
    "JobQuery",
    "JobQueueFull",
    "JobRegistrationError",
    "JobRegistry",
    "JobScope",
    "JobScopeClosed",
    "JobScopeKind",
    "JobState",
    "JobStateChange",
    "JobStateError",
    "JobStateListener",
    "LaneLimits",
    "MessageErrorFormatter",
    "ProcessFactory",
    "ProcessGroup",
    "QueueMode",
    "ScopedJobInfo",
    "ServiceDriver",
    "ServiceHealth",
    "ServiceJob",
    "SlotLease",
    "SlotMode",
    "SubprocessJob",
    "TERMINAL_STATES",
    "ThreadJob",
    "TreeTerminator",
]
