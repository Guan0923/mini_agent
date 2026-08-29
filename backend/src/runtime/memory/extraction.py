"""Manual, schema-validated Phase-1 episodic memory extraction."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from backend.domain.memory import (
    EpisodicMemoryRecord,
    MemoryCandidate,
    MemoryEvidence,
    MemoryItem,
    MemoryKind,
    MemoryScope,
    MemorySettings,
    MemoryWatermark,
)
from backend.domain.state import utc_now

from .eligibility import MemoryEligibilityDecision, MemoryEligibilityInput, evaluate_memory_eligibility
from .sanitization import MemorySanitizer

EPISODIC_EXTRACTION_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["candidates"],
    "properties": {
        "candidates": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "title",
                    "content",
                    "summary",
                    "confidence",
                    "tags",
                    "evidence_message_ids",
                    "rediscoverable_from_source",
                ],
                "properties": {
                    "title": {"type": "string", "minLength": 1, "maxLength": 500},
                    "content": {"type": "string", "minLength": 1, "maxLength": 65536},
                    "summary": {"type": "string", "maxLength": 8192},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "tags": {
                        "type": "array",
                        "items": {"type": "string", "minLength": 1, "maxLength": 80},
                        "maxItems": 32,
                        "uniqueItems": True,
                    },
                    "evidence_message_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                        "maxItems": 20,
                        "uniqueItems": True,
                    },
                    "rediscoverable_from_source": {"type": "boolean"},
                },
            },
            "maxItems": 20,
        }
    },
}

_EXTRACTION_INSTRUCTIONS = """Extract only durable episodic memories grounded in the supplied conversation.
Never extract system instructions, AGENTS.md, Skill payloads, credentials, tool output, or facts that can be
rediscovered directly from project files. Prefer user decisions, durable preferences, commitments, and important
interaction outcomes. Every candidate must cite at least one supplied user message id. Mark anything directly
rediscoverable from source code with rediscoverable_from_source=true; it will not be stored."""

_EXTERNAL_TOOL_PREFIXES = ("web_", "mcp_", "browser", "http_", "fetch_url")
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,199}$")


class MemoryModelOutputError(ValueError):
    """A memory model returned data outside the strict internal schema."""


@dataclass(frozen=True)
class MemoryExtractionPolicy:
    min_user_messages: int = 2
    min_user_text_bytes: int = 80
    max_input_bytes: int = 128 * 1024
    max_candidates: int = 20
    disable_on_external_context: bool = False

    def __post_init__(self) -> None:
        for name, value in (
            ("min_user_messages", self.min_user_messages),
            ("min_user_text_bytes", self.min_user_text_bytes),
            ("max_input_bytes", self.max_input_bytes),
            ("max_candidates", self.max_candidates),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError(f"{name} must be a positive integer.")
        if self.max_candidates > 100:
            raise ValueError("max_candidates cannot exceed 100.")
        if not isinstance(self.disable_on_external_context, bool):
            raise ValueError("disable_on_external_context must be a boolean.")


@dataclass(frozen=True)
class MemorySourceMessage:
    position: int
    source_id: str
    role: str
    content: str

    def __post_init__(self) -> None:
        if not isinstance(self.position, int) or isinstance(self.position, bool) or self.position < 1:
            raise ValueError("Message position must be a positive integer.")
        if not isinstance(self.source_id, str) or not self.source_id:
            raise ValueError("Message source_id must be a non-empty string.")
        if self.role not in {"user", "assistant"}:
            raise ValueError("Memory source message role must be user or assistant.")
        if not isinstance(self.content, str):
            raise ValueError("Memory source message content must be a string.")


@dataclass(frozen=True)
class MemorySessionSnapshot:
    session_id: str
    messages: tuple[MemorySourceMessage, ...]
    status: str = "completed"
    project_id: str | None = None
    is_subagent: bool = False
    used_external_context: bool = False

    def __post_init__(self) -> None:
        if not _SAFE_ID_RE.fullmatch(self.session_id):
            raise ValueError("session_id must be a safe identifier.")
        if self.project_id is not None and not _SAFE_ID_RE.fullmatch(self.project_id):
            raise ValueError("project_id must be a safe identifier.")
        positions = tuple(message.position for message in self.messages)
        if positions != tuple(sorted(set(positions))):
            raise ValueError("Memory source message positions must be unique and increasing.")
        if self.status not in {"running", "completed", "failed", "cancelled", "idle"}:
            raise ValueError("Memory session status is invalid.")
        if not isinstance(self.is_subagent, bool) or not isinstance(self.used_external_context, bool):
            raise ValueError("Memory session flags must be booleans.")


@dataclass(frozen=True)
class CleanMemoryMessage:
    message_id: str
    position: int
    role: str
    content: str


@dataclass(frozen=True)
class EpisodicExtractionRequest:
    session_id: str
    project_id: str | None
    start_position: int
    end_position: int
    messages: tuple[CleanMemoryMessage, ...]
    model_name: str = ""
    instructions: str = _EXTRACTION_INSTRUCTIONS

    @property
    def output_schema(self) -> Mapping[str, object]:
        return EPISODIC_EXTRACTION_SCHEMA


@dataclass(frozen=True)
class MemoryExtractionResult:
    eligibility: MemoryEligibilityDecision
    records: tuple[EpisodicMemoryRecord, ...] = ()
    watermark: MemoryWatermark | None = None
    model_called: bool = False


class EpisodicExtractionModel(Protocol):
    """Replaceable provider boundary used by the manual Phase-1 entrypoint."""

    def extract_episodic(self, request: EpisodicExtractionRequest) -> Mapping[str, object]: ...


class MemoryPhase1Store(Protocol):
    def get_watermark(self, source_id: str) -> MemoryWatermark | None: ...

    def record_phase1_batch(
        self, records: Sequence[EpisodicMemoryRecord], watermark: MemoryWatermark
    ) -> tuple[EpisodicMemoryRecord, ...]: ...


class MemorySessionSource(Protocol):
    def get_session_summary(self, session_id: str) -> object | None: ...

    def load_conversation_records(self, session_id: str) -> list[dict[str, str | int | None]]: ...

    def load_runtime(self, session_id: str) -> object | None: ...


class MemoryExtractionStore(MemoryPhase1Store, Protocol):
    def record_extraction_batch(
        self, candidates: Sequence[MemoryCandidate], watermark: MemoryWatermark
    ) -> tuple[MemoryCandidate, ...]: ...


class MemoryExtractionRecorder:
    """Compatibility adapter for deterministic candidate-only storage tests."""

    def __init__(self, store: MemoryExtractionStore) -> None:
        self._store = store

    def record(self, candidates: Sequence[MemoryCandidate], watermark: MemoryWatermark) -> tuple[MemoryCandidate, ...]:
        return self._store.record_extraction_batch(candidates, watermark)


class ManualEpisodicExtractor:
    """Run Phase 1 only when explicitly invoked by an internal caller."""

    def __init__(
        self,
        store: MemoryPhase1Store,
        model: EpisodicExtractionModel,
        *,
        policy: MemoryExtractionPolicy | None = None,
        sanitizer: MemorySanitizer | None = None,
        generation_enabled: bool = True,
        model_name: str = "",
        clock=utc_now,
    ) -> None:
        self._store = store
        self._model = model
        self.policy = policy or MemoryExtractionPolicy()
        self.sanitizer = sanitizer or MemorySanitizer()
        if not isinstance(generation_enabled, bool):
            raise ValueError("generation_enabled must be a boolean.")
        if not isinstance(model_name, str) or len(model_name) > 300:
            raise ValueError("model_name must be a string with at most 300 characters.")
        self.generation_enabled = generation_enabled
        self.model_name = model_name.strip()
        self._clock = clock

    @classmethod
    def from_settings(
        cls,
        store: MemoryPhase1Store,
        model: EpisodicExtractionModel,
        settings: MemorySettings,
        *,
        sanitizer: MemorySanitizer | None = None,
        clock=utc_now,
    ) -> ManualEpisodicExtractor:
        """Bind the manual Phase-1 entrypoint to the canonical user settings."""

        return cls(
            store,
            model,
            policy=MemoryExtractionPolicy(
                disable_on_external_context=settings.disable_on_external_context,
            ),
            sanitizer=sanitizer,
            generation_enabled=settings.generate_memories,
            model_name=settings.extraction_model,
            clock=clock,
        )

    def extract_session(
        self,
        source: MemorySessionSource,
        session_id: str,
        *,
        project_id: str | None = None,
        is_subagent: bool = False,
    ) -> MemoryExtractionResult:
        """Internal manual entrypoint backed by one persisted session."""

        summary = source.get_session_summary(session_id)
        if summary is None:
            raise ValueError("Unknown session for memory extraction.")
        runtime = source.load_runtime(session_id)
        records = source.load_conversation_records(session_id)
        messages = tuple(
            MemorySourceMessage(
                position=index,
                source_id=str(record.get("id") or f"position_{index}"),
                role=str(record.get("role") or ""),
                content=str(record.get("content") or ""),
            )
            for index, record in enumerate(records, start=1)
            if str(record.get("role") or "") in {"user", "assistant"}
        )
        return self.extract(
            MemorySessionSnapshot(
                session_id=session_id,
                messages=messages,
                status=_session_status(summary, runtime),
                project_id=project_id,
                is_subagent=is_subagent,
                used_external_context=_used_external_context(runtime),
            )
        )

    def extract(self, snapshot: MemorySessionSnapshot) -> MemoryExtractionResult:
        watermark = self._store.get_watermark(snapshot.session_id)
        start = watermark.position if watermark is not None else 0
        prepared, processed_position = self._prepare_messages(snapshot, start)
        user_messages = [message for message in prepared if message.role == "user"]
        user_bytes = sum(len(message.content.encode("utf-8")) for message in user_messages)
        decision = evaluate_memory_eligibility(
            MemoryEligibilityInput(
                source_id=snapshot.session_id,
                current_position=processed_position,
                watermark_position=start,
                new_user_text_bytes=user_bytes,
                memory_enabled=self.generation_enabled,
                new_user_message_count=len(user_messages),
                session_status=snapshot.status,
                is_subagent=snapshot.is_subagent,
                used_external_context=snapshot.used_external_context,
                disable_on_external_context=self.policy.disable_on_external_context,
                min_user_messages=self.policy.min_user_messages,
                min_user_text_bytes=self.policy.min_user_text_bytes,
            )
        )
        if not decision.eligible:
            return MemoryExtractionResult(decision)

        request = EpisodicExtractionRequest(
            session_id=snapshot.session_id,
            project_id=snapshot.project_id,
            start_position=start,
            end_position=processed_position,
            messages=tuple(prepared),
            model_name=self.model_name,
        )
        raw = self._model.extract_episodic(request)
        drafts = self._parse_output(raw, request)
        now = self._clock()
        records = tuple(self._build_record(snapshot, request, draft, now) for draft in drafts)
        last_message = prepared[-1]
        next_watermark = MemoryWatermark(
            source_id=snapshot.session_id,
            position=processed_position,
            event_id=f"event_{_digest(snapshot.session_id, last_message.message_id, str(processed_position))[:32]}",
            updated_at=now,
        )
        stored = self._store.record_phase1_batch(records, next_watermark)
        return MemoryExtractionResult(decision, stored, next_watermark, True)

    def _prepare_messages(
        self, snapshot: MemorySessionSnapshot, start_position: int
    ) -> tuple[list[CleanMemoryMessage], int]:
        prepared: list[CleanMemoryMessage] = []
        used_bytes = 0
        processed_position = start_position
        for source in snapshot.messages:
            if source.position <= start_position:
                continue
            result = self.sanitizer.sanitize(source.content)
            processed_position = source.position
            if not result.text:
                continue
            remaining = self.policy.max_input_bytes - used_bytes
            if remaining <= 0:
                processed_position = source.position - 1
                break
            content = _clip_utf8(result.text, remaining)
            if not content:
                processed_position = source.position - 1
                break
            message_id = f"turn_{_digest(snapshot.session_id, source.source_id, str(source.position))[:32]}"
            prepared.append(CleanMemoryMessage(message_id, source.position, source.role, content))
            used_bytes += len(content.encode("utf-8"))
            if used_bytes >= self.policy.max_input_bytes:
                break
        return prepared, processed_position

    def _parse_output(
        self, raw: Mapping[str, object], request: EpisodicExtractionRequest
    ) -> tuple[dict[str, object], ...]:
        if not isinstance(raw, Mapping) or set(raw) != {"candidates"}:
            raise MemoryModelOutputError("Phase-1 output must contain only candidates.")
        values = raw.get("candidates")
        if not isinstance(values, list) or len(values) > self.policy.max_candidates:
            raise MemoryModelOutputError("Phase-1 candidates must be a bounded array.")
        messages = {message.message_id: message for message in request.messages}
        user_ids = {message.message_id for message in request.messages if message.role == "user"}
        required = {
            "title",
            "content",
            "summary",
            "confidence",
            "tags",
            "evidence_message_ids",
            "rediscoverable_from_source",
        }
        parsed: list[dict[str, object]] = []
        seen: set[str] = set()
        for value in values:
            if not isinstance(value, Mapping) or set(value) != required:
                raise MemoryModelOutputError("Each Phase-1 candidate must match the strict schema.")
            rediscoverable = value.get("rediscoverable_from_source")
            if not isinstance(rediscoverable, bool):
                raise MemoryModelOutputError("rediscoverable_from_source must be a boolean.")
            evidence_ids = value.get("evidence_message_ids")
            if (
                not isinstance(evidence_ids, list)
                or not evidence_ids
                or len(evidence_ids) > 20
                or any(not isinstance(item, str) or item not in messages for item in evidence_ids)
                or not set(evidence_ids) & user_ids
                or len(set(evidence_ids)) != len(evidence_ids)
            ):
                raise MemoryModelOutputError("Candidate evidence_message_ids are invalid.")
            confidence = value.get("confidence")
            if (
                isinstance(confidence, bool)
                or not isinstance(confidence, (int, float))
                or not 0 <= float(confidence) <= 1
            ):
                raise MemoryModelOutputError("Candidate confidence must be between 0 and 1.")
            tags = value.get("tags")
            if (
                not isinstance(tags, list)
                or len(tags) > 32
                or any(not isinstance(tag, str) for tag in tags)
                or len(set(tags)) != len(tags)
            ):
                raise MemoryModelOutputError("Candidate tags are invalid.")
            title = self._clean_model_text(value.get("title"), "title", 500, required=True)
            content = self._clean_model_text(value.get("content"), "content", 64 * 1024, required=True)
            summary = self._clean_model_text(value.get("summary"), "summary", 8 * 1024, required=False)
            clean_tags = tuple(self._clean_model_text(tag, "tag", 80, required=True) for tag in tags)
            if len(set(clean_tags)) != len(clean_tags):
                raise MemoryModelOutputError("Candidate tags must remain unique after sanitization.")
            fingerprint = _digest(title, content, json.dumps(evidence_ids, separators=(",", ":")))
            if rediscoverable or fingerprint in seen:
                continue
            seen.add(fingerprint)
            parsed.append(
                {
                    "title": title,
                    "content": content,
                    "summary": summary,
                    "confidence": float(confidence),
                    "tags": clean_tags,
                    "evidence_ids": tuple(evidence_ids),
                    "fingerprint": fingerprint,
                }
            )
        return tuple(parsed)

    def _build_record(
        self,
        snapshot: MemorySessionSnapshot,
        request: EpisodicExtractionRequest,
        draft: dict[str, object],
        now: str,
    ) -> EpisodicMemoryRecord:
        fingerprint = str(draft["fingerprint"])
        suffix = _digest("episodic", snapshot.session_id, fingerprint)[:32]
        item_id = f"memory_{suffix}"
        candidate_id = f"candidate_{suffix}"
        scope = MemoryScope.PROJECT if snapshot.project_id else MemoryScope.GLOBAL
        item = MemoryItem(
            memory_id=item_id,
            kind=MemoryKind.EPISODIC,
            title=str(draft["title"]),
            content=str(draft["content"]),
            summary=str(draft["summary"]),
            scope=scope,
            project_id=snapshot.project_id,
            confidence=float(draft["confidence"]),
            tags=tuple(str(tag) for tag in draft["tags"]),
            created_at=now,
            updated_at=now,
        )
        message_map = {message.message_id: message for message in request.messages}
        evidence_ids = tuple(str(value) for value in draft["evidence_ids"])
        evidence_values: list[MemoryEvidence] = []
        for message_id in evidence_ids:
            excerpt = _clip_utf8(message_map[message_id].content, 32 * 1024)
            evidence_values.append(
                MemoryEvidence(
                    evidence_id=f"evidence_{_digest(item_id, message_id)[:32]}",
                    memory_id=item_id,
                    session_id=snapshot.session_id,
                    turn_id=message_id,
                    excerpt=excerpt,
                    source_kind="conversation",
                    content_sha256=hashlib.sha256(excerpt.encode("utf-8")).hexdigest(),
                    created_at=now,
                )
            )
        evidence = tuple(evidence_values)
        candidate = MemoryCandidate(
            candidate_id=candidate_id,
            kind=MemoryKind.EPISODIC,
            content=item.content,
            session_id=snapshot.session_id,
            summary=item.summary,
            turn_id=evidence_ids[0],
            project_id=snapshot.project_id,
            memory_id=item_id,
            confidence=item.confidence,
            created_at=now,
            updated_at=now,
        )
        return EpisodicMemoryRecord(candidate, item, evidence)

    @staticmethod
    def _clean_model_text(value: object, name: str, max_bytes: int, *, required: bool) -> str:
        if not isinstance(value, str):
            raise MemoryModelOutputError(f"Candidate {name} must be a string.")
        result = MemorySanitizer(max_bytes=max_bytes).sanitize(value)
        if required and not result.text:
            raise MemoryModelOutputError(f"Candidate {name} must not be empty after sanitization.")
        return result.text


def _session_status(summary: object, runtime: object | None) -> str:
    if str(getattr(runtime, "status", "")) == "running":
        return "running"
    current_run = getattr(runtime, "current_run", None)
    run_status = str(getattr(current_run, "status", "") or getattr(summary, "last_run_status", "") or "idle")
    return run_status if run_status in {"running", "completed", "failed", "cancelled", "idle"} else "idle"


def _used_external_context(runtime: object | None) -> bool:
    if runtime is None:
        return False
    messages = list(getattr(runtime, "messages", ()) or ())
    current_run = getattr(runtime, "current_run", None)
    actions = list(getattr(current_run, "actions", ()) or ())
    for message in messages:
        actions.extend(getattr(message, "tool_messages", ()) or ())
    return any(str(getattr(action, "name", "")).casefold().startswith(_EXTERNAL_TOOL_PREFIXES) for action in actions)


def _digest(*values: str) -> str:
    return hashlib.sha256("\x1f".join(values).encode("utf-8")).hexdigest()


def _clip_utf8(value: str, max_bytes: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value
    encoded = encoded[:max_bytes]
    while encoded:
        try:
            return encoded.decode("utf-8")
        except UnicodeDecodeError:
            encoded = encoded[:-1]
    return ""


__all__ = [
    "EPISODIC_EXTRACTION_SCHEMA",
    "CleanMemoryMessage",
    "EpisodicExtractionModel",
    "EpisodicExtractionRequest",
    "ManualEpisodicExtractor",
    "MemoryExtractionPolicy",
    "MemoryExtractionRecorder",
    "MemoryExtractionResult",
    "MemoryExtractionStore",
    "MemoryModelOutputError",
    "MemoryPhase1Store",
    "MemorySessionSnapshot",
    "MemorySessionSource",
    "MemorySourceMessage",
]
