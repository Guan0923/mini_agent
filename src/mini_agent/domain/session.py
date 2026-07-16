"""Domain values for persistent multi-turn agent sessions."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

DEFAULT_SESSION_TITLE = "New session"


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


@dataclass(frozen=True)
class SessionSummary:
    """Session metadata used by listing and the TUI status view."""

    session_id: str
    title: str
    created_at: str
    updated_at: str
    message_count: int
    last_run_id: str | None = None
    last_run_status: str | None = None
