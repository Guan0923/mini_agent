"""Domain values for persistent multi-turn agent sessions."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

DEFAULT_SESSION_TITLE = "New session"

DEFAULT_TIME_ZONE = "Asia/Shanghai"


@dataclass(frozen=True)
class TimeZoneOption:
    """One time zone exposed by the interactive session selector."""

    identifier: str
    label: str


TIME_ZONE_OPTIONS = (
    TimeZoneOption("UTC", "UTC"),
    TimeZoneOption("Asia/Shanghai", "Shanghai"),
    TimeZoneOption("Asia/Tokyo", "Tokyo"),
    TimeZoneOption("Asia/Singapore", "Singapore"),
    TimeZoneOption("Europe/London", "London"),
    TimeZoneOption("Europe/Paris", "Paris"),
    TimeZoneOption("America/New_York", "New York"),
    TimeZoneOption("America/Los_Angeles", "Los Angeles"),
)

SUPPORTED_TIME_ZONES = frozenset(option.identifier for option in TIME_ZONE_OPTIONS)


def validate_time_zone(timezone: str) -> str:
    """Return one supported IANA zone or raise a user-facing validation error."""

    if timezone not in SUPPORTED_TIME_ZONES:
        raise ValueError(f"Unsupported time zone: {timezone}")
    return timezone


def new_session_id() -> str:
    """Create a stable, human-identifiable session identifier."""

    return f"session_{uuid4().hex}"


@dataclass(frozen=True)
class Session:
    """A persistent conversation container scoped to one workspace."""

    session_id: str
    title: str
    created_at: str
    updated_at: str
    client_id: str | None = None
    archived_at: str | None = None
    deleted_at: str | None = None
    local_only: bool = False
    title_is_custom: bool = False


@dataclass(frozen=True)
class SessionSummary:
    """Session metadata used by listing and the TUI status view."""

    session_id: str
    title: str
    created_at: str
    updated_at: str
    # Sidebar count: only persisted user and assistant messages.
    message_count: int
    last_run_id: str | None = None
    last_run_status: str | None = None
    client_id: str | None = None
    archived_at: str | None = None
    deleted_at: str | None = None
    last_node_id: str | None = None
    local_only: bool = False
    title_is_custom: bool = False

    @property
    def is_active(self) -> bool:
        return self.archived_at is None and self.deleted_at is None

    @property
    def is_archived(self) -> bool:
        return self.archived_at is not None and self.deleted_at is None

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None


@dataclass(frozen=True)
class ResumePreview:
    """User-facing provenance for selecting a durable session or interrupted run."""

    session_id: str
    title: str
    workspace_root: str | None
    workflow_id: str | None
    run_id: str | None
    attempt: int | None
    task: str | None
    mode: str | None
    strategy: str | None
    status: str
    stop_reason: str | None = None
    source_session_id: str | None = None
    source_run_id: str | None = None
    checkpoint_reason: str | None = None
    checkpoint_at: str | None = None
    interruption_reason: str | None = None
    indeterminate_call_ids: tuple[str, ...] = ()

    @property
    def requires_action(self) -> bool:
        return self.status in {"running", "failed", "cancelled"}
