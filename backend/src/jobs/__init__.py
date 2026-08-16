"""Neutral job lifecycle contract: stable public types and the Job base class.

Importing ``backend.jobs`` loads no runtime, tools, MCP, API, storage, or
third-party modules; later modules build registries and carriers on top of
these primitives without reimplementing the state machine.
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
from .safety import ClassNameErrorFormatter, ErrorFormatter

__all__ = [
    "ClassNameErrorFormatter",
    "Clock",
    "ErrorFormatter",
    "Job",
    "JobInfo",
    "JobKind",
    "JobState",
    "JobStateChange",
    "JobStateError",
    "JobStateListener",
    "TERMINAL_STATES",
]
