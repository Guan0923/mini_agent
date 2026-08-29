"""Provider-agnostic memory values and lifecycle constraints."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, StrEnum
from uuid import uuid4

from .state import utc_now

_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,199}$")


class MemoryKind(StrEnum):
    EPISODIC = "episodic"
    SEMANTIC = "semantic"
    PROCEDURAL = "procedural"


class MemoryScope(StrEnum):
    GLOBAL = "global"
    PROJECT = "project"


class MemoryStatus(StrEnum):
    ACTIVE = "active"
    DISABLED = "disabled"
    SUPERSEDED = "superseded"
    DELETED = "deleted"


class MemoryCandidateStatus(StrEnum):
    PENDING = "pending"
    SELECTED = "selected"
    REJECTED = "rejected"


class MemoryJobKind(StrEnum):
    EXTRACT = "extract"
    CONSOLIDATE = "consolidate"
    REBUILD_PROJECTIONS = "rebuild_projections"


class MemoryJobStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class MemorySettings:
    """Validated per-user controls for memory production and prompt use."""

    use_memories: bool = False
    generate_memories: bool = False
    automatic_memory_enabled: bool = False
    disable_on_external_context: bool = True
    extraction_model: str = ""
    consolidation_model: str = ""
    retrieval_limit: int = 40
    injection_max_items: int = 8
    injection_max_tokens: int = 1200
    injection_max_bytes: int = 8192

    def __post_init__(self) -> None:
        for name in (
            "use_memories",
            "generate_memories",
            "automatic_memory_enabled",
            "disable_on_external_context",
        ):
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f"{name} must be a boolean.")
        if self.automatic_memory_enabled and not self.generate_memories:
            raise ValueError("automatic_memory_enabled requires generate_memories.")
        for name in ("extraction_model", "consolidation_model"):
            value = getattr(self, name)
            if not isinstance(value, str) or len(value) > 300:
                raise ValueError(f"{name} must be a string with at most 300 characters.")
        _require_bounded_int(self.retrieval_limit, "retrieval_limit", minimum=1, maximum=200)
        _require_bounded_int(self.injection_max_items, "injection_max_items", minimum=1, maximum=50)
        _require_bounded_int(self.injection_max_tokens, "injection_max_tokens", minimum=128, maximum=16_000)
        _require_bounded_int(self.injection_max_bytes, "injection_max_bytes", minimum=512, maximum=65_536)

    @classmethod
    def from_mapping(cls, values: Mapping[str, object] | None = None) -> MemorySettings:
        """Merge a partial untrusted mapping over safe, opt-in defaults."""

        raw = values or {}
        defaults = cls()
        return cls(
            use_memories=raw.get("use_memories", defaults.use_memories),  # type: ignore[arg-type]
            generate_memories=raw.get("generate_memories", defaults.generate_memories),  # type: ignore[arg-type]
            automatic_memory_enabled=raw.get(  # type: ignore[arg-type]
                "automatic_memory_enabled", defaults.automatic_memory_enabled
            ),
            disable_on_external_context=raw.get(  # type: ignore[arg-type]
                 "disable_on_external_context", defaults.disable_on_external_context
            ),
            extraction_model=_clean_model_selector(raw.get("extraction_model", defaults.extraction_model)),
            consolidation_model=_clean_model_selector(raw.get("consolidation_model", defaults.consolidation_model)),
            retrieval_limit=raw.get("retrieval_limit", defaults.retrieval_limit),  # type: ignore[arg-type]
            injection_max_items=raw.get("injection_max_items", defaults.injection_max_items),  # type: ignore[arg-type]
            injection_max_tokens=raw.get(  # type: ignore[arg-type]
                "injection_max_tokens", defaults.injection_max_tokens
            ),
            injection_max_bytes=raw.get("injection_max_bytes", defaults.injection_max_bytes),  # type: ignore[arg-type]
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "use_memories": self.use_memories,
            "generate_memories": self.generate_memories,
            "automatic_memory_enabled": self.automatic_memory_enabled,
            "disable_on_external_context": self.disable_on_external_context,
            "extraction_model": self.extraction_model,
            "consolidation_model": self.consolidation_model,
            "retrieval_limit": self.retrieval_limit,
            "injection_max_items": self.injection_max_items,
            "injection_max_tokens": self.injection_max_tokens,
            "injection_max_bytes": self.injection_max_bytes,
        }


@dataclass(frozen=True)
class MemoryItem:
    """One durable memory record stored in the authoritative database."""

    memory_id: str
    kind: MemoryKind
    title: str
    content: str
    summary: str = ""
    scope: MemoryScope = MemoryScope.GLOBAL
    project_id: str | None = None
    confidence: float = 1.0
    tags: tuple[str, ...] = ()
    status: MemoryStatus = MemoryStatus.ACTIVE
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    last_used_at: str | None = None
    deleted_at: str | None = None

    def __post_init__(self) -> None:
        _require_safe_id(self.memory_id, "memory_id")
        _require_enum(self.kind, MemoryKind, "kind")
        _require_text(self.title, "title", max_length=500)
        _require_text(self.content, "content", max_length=64 * 1024)
        _require_optional_text(self.summary, "summary", max_length=8 * 1024)
        _require_enum(self.scope, MemoryScope, "scope")
        _validate_scope(self.scope, self.project_id)
        _require_confidence(self.confidence)
        _require_tags(self.tags)
        _require_enum(self.status, MemoryStatus, "status")
        _require_timestamp(self.created_at, "created_at")
        _require_timestamp(self.updated_at, "updated_at")
        _require_optional_timestamp(self.last_used_at, "last_used_at")
        _require_optional_timestamp(self.deleted_at, "deleted_at")
        if self.status is MemoryStatus.DELETED and not self.deleted_at:
            raise ValueError("Deleted memory items require deleted_at.")
        if self.status is not MemoryStatus.DELETED and self.deleted_at is not None:
            raise ValueError("Only deleted memory items may set deleted_at.")

    @classmethod
    def new(
        cls,
        *,
        kind: MemoryKind,
        title: str,
        content: str,
        summary: str = "",
        scope: MemoryScope = MemoryScope.GLOBAL,
        project_id: str | None = None,
        confidence: float = 1.0,
        tags: tuple[str, ...] = (),
    ) -> MemoryItem:
        now = utc_now()
        return cls(
            memory_id=f"memory_{uuid4().hex}",
            kind=kind,
            title=title,
            content=content,
            summary=summary,
            scope=scope,
            project_id=project_id,
            confidence=confidence,
            tags=tags,
            created_at=now,
            updated_at=now,
        )


@dataclass(frozen=True)
class MemoryEvidence:
    """A traceable source excerpt supporting one durable memory."""

    evidence_id: str
    memory_id: str
    session_id: str
    excerpt: str
    turn_id: str | None = None
    source_kind: str = "conversation"
    content_sha256: str = ""
    created_at: str = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        _require_safe_id(self.evidence_id, "evidence_id")
        _require_safe_id(self.memory_id, "memory_id")
        _require_safe_id(self.session_id, "session_id")
        if self.turn_id is not None:
            _require_safe_id(self.turn_id, "turn_id")
        _require_text(self.excerpt, "excerpt", max_length=32 * 1024)
        _require_text(self.source_kind, "source_kind", max_length=80)
        if self.content_sha256 and not re.fullmatch(r"[0-9a-f]{64}", self.content_sha256):
            raise ValueError("content_sha256 must be a lower-case SHA-256 digest.")
        _require_timestamp(self.created_at, "created_at")

    @classmethod
    def new(
        cls,
        *,
        memory_id: str,
        session_id: str,
        excerpt: str,
        turn_id: str | None = None,
        source_kind: str = "conversation",
        content_sha256: str = "",
    ) -> MemoryEvidence:
        return cls(
            evidence_id=f"evidence_{uuid4().hex}",
            memory_id=memory_id,
            session_id=session_id,
            turn_id=turn_id,
            excerpt=excerpt,
            source_kind=source_kind,
            content_sha256=content_sha256,
        )


@dataclass(frozen=True)
class MemoryCandidate:
    """One Phase-1 extraction candidate awaiting consolidation."""

    candidate_id: str
    kind: MemoryKind
    content: str
    session_id: str
    summary: str = ""
    turn_id: str | None = None
    project_id: str | None = None
    memory_id: str | None = None
    confidence: float = 1.0
    status: MemoryCandidateStatus = MemoryCandidateStatus.PENDING
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        _require_safe_id(self.candidate_id, "candidate_id")
        _require_enum(self.kind, MemoryKind, "kind")
        _require_text(self.content, "content", max_length=64 * 1024)
        _require_text(self.session_id, "session_id", max_length=200)
        _require_safe_id(self.session_id, "session_id")
        _require_optional_text(self.summary, "summary", max_length=8 * 1024)
        if self.turn_id is not None:
            _require_safe_id(self.turn_id, "turn_id")
        if self.project_id is not None:
            _require_safe_id(self.project_id, "project_id")
        if self.memory_id is not None:
            _require_safe_id(self.memory_id, "memory_id")
        _require_confidence(self.confidence)
        _require_enum(self.status, MemoryCandidateStatus, "status")
        _require_timestamp(self.created_at, "created_at")
        _require_timestamp(self.updated_at, "updated_at")

    @classmethod
    def new(
        cls,
        *,
        kind: MemoryKind,
        content: str,
        session_id: str,
        summary: str = "",
        turn_id: str | None = None,
        project_id: str | None = None,
        memory_id: str | None = None,
        confidence: float = 1.0,
    ) -> MemoryCandidate:
        now = utc_now()
        return cls(
            candidate_id=f"candidate_{uuid4().hex}",
            kind=kind,
            content=content,
            session_id=session_id,
            summary=summary,
            turn_id=turn_id,
            project_id=project_id,
            memory_id=memory_id,
            confidence=confidence,
            created_at=now,
            updated_at=now,
        )


@dataclass(frozen=True)
class MemoryWatermark:
    """Monotonic source position used for incremental extraction."""

    source_id: str
    position: int
    event_id: str | None = None
    updated_at: str = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        _require_safe_id(self.source_id, "source_id")
        if not isinstance(self.position, int) or isinstance(self.position, bool) or self.position < 0:
            raise ValueError("Watermark position must be a non-negative integer.")
        if self.event_id is not None:
            _require_safe_id(self.event_id, "event_id")
        _require_timestamp(self.updated_at, "updated_at")


@dataclass(frozen=True)
class MemoryJob:
    """Persisted extraction, consolidation, or projection work."""

    job_id: str
    kind: MemoryJobKind
    status: MemoryJobStatus = MemoryJobStatus.PENDING
    source_id: str | None = None
    project_id: str | None = None
    attempts: int = 0
    max_attempts: int = 3
    available_at: str = field(default_factory=utc_now)
    lease_owner: str | None = None
    lease_expires_at: str | None = None
    last_error: str = ""
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        _require_safe_id(self.job_id, "job_id")
        _require_enum(self.kind, MemoryJobKind, "kind")
        _require_enum(self.status, MemoryJobStatus, "status")
        if self.source_id is not None:
            _require_safe_id(self.source_id, "source_id")
        if self.project_id is not None:
            _require_safe_id(self.project_id, "project_id")
        if not isinstance(self.attempts, int) or isinstance(self.attempts, bool) or self.attempts < 0:
            raise ValueError("Job attempts must be a non-negative integer.")
        if not isinstance(self.max_attempts, int) or isinstance(self.max_attempts, bool) or self.max_attempts < 1:
            raise ValueError("Job max_attempts must be a positive integer.")
        if self.attempts > self.max_attempts:
            raise ValueError("Job attempts cannot exceed max_attempts.")
        if self.lease_owner is not None:
            _require_safe_id(self.lease_owner, "lease_owner")
        _require_timestamp(self.available_at, "available_at")
        _require_optional_timestamp(self.lease_expires_at, "lease_expires_at")
        _require_optional_text(self.last_error, "last_error", max_length=4 * 1024)
        _require_timestamp(self.created_at, "created_at")
        _require_timestamp(self.updated_at, "updated_at")
        if self.status is MemoryJobStatus.RUNNING and (not self.lease_owner or not self.lease_expires_at):
            raise ValueError("Running memory jobs require an owner and lease expiry.")
        if self.status is not MemoryJobStatus.RUNNING and (
            self.lease_owner is not None or self.lease_expires_at is not None
        ):
            raise ValueError("Only running memory jobs may hold a lease.")

    @classmethod
    def new(
        cls,
        *,
        kind: MemoryJobKind,
        source_id: str | None = None,
        project_id: str | None = None,
        max_attempts: int = 3,
        available_at: str | None = None,
    ) -> MemoryJob:
        now = utc_now()
        return cls(
            job_id=f"memory_job_{uuid4().hex}",
            kind=kind,
            source_id=source_id,
            project_id=project_id,
            max_attempts=max_attempts,
            available_at=available_at or now,
            created_at=now,
            updated_at=now,
        )


@dataclass(frozen=True)
class MemorySearchResult:
    item: MemoryItem
    rank: float


@dataclass(frozen=True)
class MemorySelectionDiff:
    """One consolidation decision applied transactionally."""

    added: tuple[MemoryItem, ...] = ()
    retained_ids: tuple[str, ...] = ()
    removed_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        retained = set(self.retained_ids)
        removed = set(self.removed_ids)
        if len(retained) != len(self.retained_ids) or len(removed) != len(self.removed_ids):
            raise ValueError("Selection diff contains duplicate memory ids.")
        for memory_id in (*retained, *removed):
            _require_safe_id(memory_id, "memory_id")
        added_ids = {item.memory_id for item in self.added}
        if len(added_ids) != len(self.added):
            raise ValueError("Selection diff contains duplicate added memory ids.")
        if added_ids & (retained | removed) or retained & removed:
            raise ValueError("Selection diff categories must be disjoint.")


@dataclass(frozen=True)
class EpisodicMemoryRecord:
    """One Phase-1 candidate, its episodic item, and traceable evidence."""

    candidate: MemoryCandidate
    item: MemoryItem
    evidence: tuple[MemoryEvidence, ...]

    def __post_init__(self) -> None:
        if self.candidate.kind is not MemoryKind.EPISODIC:
            raise ValueError("Phase-1 candidates must be episodic.")
        if self.item.kind is not MemoryKind.EPISODIC:
            raise ValueError("Phase-1 items must be episodic.")
        if self.candidate.memory_id != self.item.memory_id:
            raise ValueError("Phase-1 candidate must reference its episodic item.")
        if self.candidate.status is not MemoryCandidateStatus.PENDING:
            raise ValueError("Phase-1 candidates must be pending.")
        if not self.evidence:
            raise ValueError("Phase-1 episodic memories require evidence.")
        if any(source.memory_id != self.item.memory_id for source in self.evidence):
            raise ValueError("Phase-1 evidence must reference its episodic item.")
        if any(source.session_id != self.candidate.session_id for source in self.evidence):
            raise ValueError("Phase-1 evidence must come from the candidate session.")


def _require_safe_id(value: str, name: str) -> None:
    if not isinstance(value, str) or not _SAFE_ID_RE.fullmatch(value) or value in {".", ".."}:
        raise ValueError(f"{name} must be a safe identifier.")


def _require_text(value: str, name: str, *, max_length: int) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string.")
    if len(value.encode("utf-8")) > max_length:
        raise ValueError(f"{name} exceeds its UTF-8 byte limit.")


def _require_optional_text(value: str, name: str, *, max_length: int) -> None:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string.")
    if len(value.encode("utf-8")) > max_length:
        raise ValueError(f"{name} exceeds its UTF-8 byte limit.")


def _require_enum(value: object, expected: type[Enum], name: str) -> None:
    if not isinstance(value, expected):
        raise ValueError(f"{name} must be a {expected.__name__} value.")


def _require_confidence(value: float) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("confidence must be a finite number.")
    if not 0 <= float(value) <= 1:
        raise ValueError("confidence must be between 0 and 1.")


def _require_bounded_int(value: int, name: str, *, minimum: int, maximum: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}.")


def _clean_model_selector(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("Memory model selectors must be strings.")
    return value.strip()


def _require_tags(tags: tuple[str, ...]) -> None:
    if not isinstance(tags, tuple) or len(tags) > 32:
        raise ValueError("tags must be a tuple with at most 32 entries.")
    if len(set(tags)) != len(tags):
        raise ValueError("tags must not contain duplicates.")
    for tag in tags:
        _require_text(tag, "tag", max_length=80)


def _validate_scope(scope: MemoryScope, project_id: str | None) -> None:
    if scope is MemoryScope.PROJECT:
        if project_id is None:
            raise ValueError("Project-scoped memories require project_id.")
        _require_safe_id(project_id, "project_id")
    elif project_id is not None:
        raise ValueError("Global memories cannot set project_id.")


def _require_timestamp(value: str, name: str) -> None:
    _require_text(value, name, max_length=100)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an ISO-8601 timestamp.") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{name} must include a time zone.")
    if parsed.utcoffset().total_seconds() != 0 or parsed.isoformat() != value:
        raise ValueError(f"{name} must use canonical UTC ISO-8601 format.")


def _require_optional_timestamp(value: str | None, name: str) -> None:
    if value is not None:
        _require_timestamp(value, name)


__all__ = [
    "EpisodicMemoryRecord",
    "MemoryCandidate",
    "MemoryCandidateStatus",
    "MemoryEvidence",
    "MemoryItem",
    "MemoryJob",
    "MemoryJobKind",
    "MemoryJobStatus",
    "MemoryKind",
    "MemoryScope",
    "MemorySearchResult",
    "MemorySelectionDiff",
    "MemorySettings",
    "MemoryStatus",
    "MemoryWatermark",
]
