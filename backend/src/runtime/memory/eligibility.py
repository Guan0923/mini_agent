"""Deterministic eligibility checks for incremental memory processing."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

_SAFE_SOURCE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,199}$")


class MemoryEligibilityReason(StrEnum):
    ELIGIBLE = "eligible"
    DISABLED = "disabled"
    NO_NEW_EVENTS = "no_new_events"
    NO_USER_CONTENT = "no_user_content"
    RUNNING = "running"
    TOO_SHORT = "too_short"
    SUBAGENT = "subagent"
    EXTERNAL_CONTEXT = "external_context"


@dataclass(frozen=True)
class MemoryEligibilityInput:
    """Bounded facts needed to decide whether one source should be extracted."""

    source_id: str
    current_position: int
    watermark_position: int | None
    new_user_text_bytes: int
    memory_enabled: bool = True
    new_user_message_count: int = 1
    session_status: str = "completed"
    is_subagent: bool = False
    used_external_context: bool = False
    disable_on_external_context: bool = False
    min_user_messages: int = 1
    min_user_text_bytes: int = 1

    def __post_init__(self) -> None:
        if not _SAFE_SOURCE_ID.fullmatch(self.source_id):
            raise ValueError("source_id must be a safe identifier.")
        for name, value in (
            ("current_position", self.current_position),
            ("new_user_text_bytes", self.new_user_text_bytes),
            ("new_user_message_count", self.new_user_message_count),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer.")
        if self.watermark_position is not None and (
            not isinstance(self.watermark_position, int)
            or isinstance(self.watermark_position, bool)
            or self.watermark_position < 0
        ):
            raise ValueError("watermark_position must be a non-negative integer or None.")
        if not isinstance(self.memory_enabled, bool):
            raise ValueError("memory_enabled must be a boolean.")
        if self.session_status not in {"running", "completed", "failed", "cancelled", "idle"}:
            raise ValueError("session_status is invalid.")
        for name, value in (
            ("is_subagent", self.is_subagent),
            ("used_external_context", self.used_external_context),
            ("disable_on_external_context", self.disable_on_external_context),
        ):
            if not isinstance(value, bool):
                raise ValueError(f"{name} must be a boolean.")
        for name, value in (
            ("min_user_messages", self.min_user_messages),
            ("min_user_text_bytes", self.min_user_text_bytes),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError(f"{name} must be a positive integer.")


@dataclass(frozen=True)
class MemoryEligibilityDecision:
    eligible: bool
    reason: MemoryEligibilityReason
    start_position: int
    end_position: int


def evaluate_memory_eligibility(value: MemoryEligibilityInput) -> MemoryEligibilityDecision:
    """Return a pure eligibility decision without reading storage or invoking a model."""

    start = value.watermark_position or 0
    if not value.memory_enabled:
        return MemoryEligibilityDecision(False, MemoryEligibilityReason.DISABLED, start, value.current_position)
    if value.is_subagent:
        return MemoryEligibilityDecision(False, MemoryEligibilityReason.SUBAGENT, start, value.current_position)
    if value.session_status == "running":
        return MemoryEligibilityDecision(False, MemoryEligibilityReason.RUNNING, start, value.current_position)
    if value.disable_on_external_context and value.used_external_context:
        return MemoryEligibilityDecision(False, MemoryEligibilityReason.EXTERNAL_CONTEXT, start, value.current_position)
    if value.current_position <= start:
        return MemoryEligibilityDecision(False, MemoryEligibilityReason.NO_NEW_EVENTS, start, value.current_position)
    if value.new_user_text_bytes == 0:
        return MemoryEligibilityDecision(False, MemoryEligibilityReason.NO_USER_CONTENT, start, value.current_position)
    if value.new_user_message_count < value.min_user_messages or value.new_user_text_bytes < value.min_user_text_bytes:
        return MemoryEligibilityDecision(False, MemoryEligibilityReason.TOO_SHORT, start, value.current_position)
    return MemoryEligibilityDecision(True, MemoryEligibilityReason.ELIGIBLE, start, value.current_position)


__all__ = [
    "MemoryEligibilityDecision",
    "MemoryEligibilityInput",
    "MemoryEligibilityReason",
    "evaluate_memory_eligibility",
]
