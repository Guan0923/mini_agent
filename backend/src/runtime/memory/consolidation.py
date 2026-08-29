"""Manual Phase-2 consolidation, conflict decisions, and soft forgetting."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from backend.domain.memory import (
    EpisodicMemoryRecord,
    MemoryEvidence,
    MemoryItem,
    MemoryKind,
    MemoryScope,
    MemorySelectionDiff,
    MemorySettings,
)
from backend.domain.state import utc_now

from .extraction import MemoryModelOutputError
from .sanitization import MemorySanitizer

MEMORY_CONSOLIDATION_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["added", "retained", "removed", "rejected_candidate_ids"],
    "properties": {
        "added": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "kind",
                    "title",
                    "content",
                    "summary",
                    "scope",
                    "project_id",
                    "confidence",
                    "tags",
                    "candidate_ids",
                ],
                "properties": {
                    "kind": {"type": "string", "enum": ["semantic", "procedural"]},
                    "title": {"type": "string", "minLength": 1, "maxLength": 500},
                    "content": {"type": "string", "minLength": 1, "maxLength": 65536},
                    "summary": {"type": "string", "maxLength": 8192},
                    "scope": {"type": "string", "enum": ["global", "project"]},
                    "project_id": {"type": ["string", "null"]},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "tags": {
                        "type": "array",
                        "items": {"type": "string", "minLength": 1, "maxLength": 80},
                        "maxItems": 32,
                        "uniqueItems": True,
                    },
                    "candidate_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                        "uniqueItems": True,
                    },
                },
            },
        },
        "retained": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["memory_id", "candidate_ids"],
                "properties": {
                    "memory_id": {"type": "string"},
                    "candidate_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                        "uniqueItems": True,
                    },
                },
            },
        },
        "removed": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["memory_id", "candidate_ids"],
                "properties": {
                    "memory_id": {"type": "string"},
                    "candidate_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                        "uniqueItems": True,
                    },
                },
            },
        },
        "rejected_candidate_ids": {
            "type": "array",
            "items": {"type": "string"},
            "uniqueItems": True,
        },
    },
}

_CONSOLIDATION_INSTRUCTIONS = """Consolidate episodic candidates across sessions into durable memory.
Semantic memories are stable facts, user preferences, and project knowledge. Procedural memories are reusable
workflow suggestions only: they must never be executed automatically and must never be converted into Skills.
Retain an existing memory when new evidence supports the same fact. Remove an existing memory when new evidence
corrects or invalidates it; removal is soft and auditable. Every candidate must be selected through added/retained
or explicitly rejected. Never preserve credentials, AGENTS.md, Skill payloads, or source-rediscoverable facts."""


@dataclass(frozen=True)
class ConsolidationCandidateView:
    candidate_id: str
    episodic_memory_id: str
    content: str
    summary: str
    project_id: str | None
    confidence: float
    evidence: tuple[MemoryEvidence, ...]


@dataclass(frozen=True)
class ExistingMemoryView:
    memory_id: str
    kind: MemoryKind
    title: str
    content: str
    summary: str
    scope: MemoryScope
    project_id: str | None
    confidence: float
    tags: tuple[str, ...]


@dataclass(frozen=True)
class MemoryConsolidationRequest:
    project_id: str | None
    candidates: tuple[ConsolidationCandidateView, ...]
    existing: tuple[ExistingMemoryView, ...]
    model_name: str = ""
    instructions: str = _CONSOLIDATION_INSTRUCTIONS

    @property
    def output_schema(self) -> Mapping[str, object]:
        return MEMORY_CONSOLIDATION_SCHEMA


@dataclass(frozen=True)
class MemoryConsolidationResult:
    raw_projection: Path | None
    rollout_summaries: tuple[Path, ...] = ()
    selection: MemorySelectionDiff = MemorySelectionDiff()
    selected_candidate_ids: tuple[str, ...] = ()
    rejected_candidate_ids: tuple[str, ...] = ()
    model_called: bool = False


class MemoryConsolidationModel(Protocol):
    """Replaceable provider boundary used by the manual Phase-2 entrypoint."""

    def consolidate_memories(self, request: MemoryConsolidationRequest) -> Mapping[str, object]: ...


class MemoryConsolidationStore(Protocol):
    def apply_selection_diff(self, selection: MemorySelectionDiff) -> None: ...

    def rebuild_projections(self) -> tuple[Path, tuple[Path, ...]]: ...


class MemoryPhase2Store(MemoryConsolidationStore, Protocol):
    def list_phase1_records(self, *, limit: int = 100) -> list[EpisodicMemoryRecord]: ...

    def list_items(
        self,
        *,
        project_id: str | None = None,
        kinds: Sequence[MemoryKind] = (),
        include_deleted: bool = False,
        limit: int = 100,
    ) -> list[MemoryItem]: ...

    def apply_consolidation_batch(
        self,
        selection: MemorySelectionDiff,
        evidence: Sequence[MemoryEvidence],
        *,
        selected_candidate_ids: Sequence[str],
        rejected_candidate_ids: Sequence[str],
    ) -> None: ...


class MemoryConsolidator:
    """Compatibility adapter for applying a precomputed selection diff."""

    def __init__(self, store: MemoryConsolidationStore) -> None:
        self._store = store

    def apply(self, selection: MemorySelectionDiff, *, rebuild_projections: bool = True) -> MemoryConsolidationResult:
        self._store.apply_selection_diff(selection)
        if not rebuild_projections:
            return MemoryConsolidationResult(None, selection=selection)
        raw, rollouts = self._store.rebuild_projections()
        return MemoryConsolidationResult(raw, rollouts, selection)


class ManualMemoryConsolidator:
    """Run Phase 2 only when explicitly invoked by an internal caller."""

    def __init__(
        self,
        store: MemoryPhase2Store,
        model: MemoryConsolidationModel,
        *,
        generation_enabled: bool = True,
        model_name: str = "",
        clock=utc_now,
    ) -> None:
        self._store = store
        self._model = model
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
        store: MemoryPhase2Store,
        model: MemoryConsolidationModel,
        settings: MemorySettings,
        *,
        clock=utc_now,
    ) -> ManualMemoryConsolidator:
        """Bind the manual Phase-2 entrypoint to the canonical user settings."""

        return cls(
            store,
            model,
            generation_enabled=settings.generate_memories,
            model_name=settings.consolidation_model,
            clock=clock,
        )

    def consolidate(self, *, project_id: str | None = None, limit: int = 100) -> MemoryConsolidationResult:
        if not self.generation_enabled:
            return MemoryConsolidationResult(None)
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000.")
        records = [
            record
            for record in self._store.list_phase1_records(limit=100_000)
            if record.candidate.project_id == project_id
        ][:limit]
        if not records:
            return MemoryConsolidationResult(None)
        existing_items = [
            item
            for item in self._store.list_items(
                project_id=project_id,
                kinds=(MemoryKind.SEMANTIC, MemoryKind.PROCEDURAL),
                limit=100_000,
            )
            if item.project_id == project_id
        ]
        request = MemoryConsolidationRequest(
            project_id=project_id,
            candidates=tuple(
                ConsolidationCandidateView(
                    candidate_id=record.candidate.candidate_id,
                    episodic_memory_id=record.item.memory_id,
                    content=record.item.content,
                    summary=record.item.summary,
                    project_id=record.candidate.project_id,
                    confidence=record.candidate.confidence,
                    evidence=record.evidence,
                )
                for record in records
            ),
            existing=tuple(_existing_view(item) for item in existing_items),
            model_name=self.model_name,
        )
        raw = self._model.consolidate_memories(request)
        decision = self._parse_output(raw, request, records, existing_items)
        self._store.apply_consolidation_batch(
            decision.selection,
            decision.evidence,
            selected_candidate_ids=decision.selected_candidate_ids,
            rejected_candidate_ids=decision.rejected_candidate_ids,
        )
        raw_path, rollouts = self._store.rebuild_projections()
        return MemoryConsolidationResult(
            raw_path,
            rollouts,
            decision.selection,
            decision.selected_candidate_ids,
            decision.rejected_candidate_ids,
            True,
        )

    def _parse_output(
        self,
        raw: Mapping[str, object],
        request: MemoryConsolidationRequest,
        records: Sequence[EpisodicMemoryRecord],
        existing_items: Sequence[MemoryItem],
    ) -> _ParsedConsolidation:
        required_root = {"added", "retained", "removed", "rejected_candidate_ids"}
        if not isinstance(raw, Mapping) or set(raw) != required_root:
            raise MemoryModelOutputError("Phase-2 output must match the strict root schema.")
        for field in required_root:
            if not isinstance(raw.get(field), list):
                raise MemoryModelOutputError(f"Phase-2 {field} must be an array.")

        candidate_map = {record.candidate.candidate_id: record for record in records}
        existing_map = {item.memory_id: item for item in existing_items}
        added: list[MemoryItem] = []
        retained_ids: list[str] = []
        removed_ids: list[str] = []
        rejected_ids = _unique_string_list(raw["rejected_candidate_ids"], "rejected_candidate_ids")
        selected_ids: set[str] = set()
        evidence: list[MemoryEvidence] = []
        evidence_keys: set[tuple[str, str, str | None, str]] = set()
        now = self._clock()

        added_signatures: set[tuple[str, str, str | None, str]] = set()
        for value in raw["added"]:  # type: ignore[index]
            item, candidate_ids = self._parse_added(value, request, candidate_map, existing_items, now)
            signature = (item.kind.value, item.scope.value, item.project_id, _normalize(item.content))
            if signature in added_signatures:
                raise MemoryModelOutputError("Phase-2 output contains duplicate added memories.")
            added_signatures.add(signature)
            added.append(item)
            selected_ids.update(candidate_ids)
            evidence.extend(self._copy_evidence(item.memory_id, candidate_ids, candidate_map, evidence_keys, now))

        retained_required = {"memory_id", "candidate_ids"}
        for value in raw["retained"]:  # type: ignore[index]
            if not isinstance(value, Mapping) or set(value) != retained_required:
                raise MemoryModelOutputError("Each retained decision must contain memory_id and candidate_ids.")
            memory_id = value.get("memory_id")
            if not isinstance(memory_id, str) or memory_id not in existing_map:
                raise MemoryModelOutputError("Retained decision references an unknown long-term memory.")
            if memory_id in retained_ids:
                raise MemoryModelOutputError("Retained memory ids must be unique.")
            candidate_ids = _candidate_ids(value.get("candidate_ids"), candidate_map)
            retained_ids.append(memory_id)
            selected_ids.update(candidate_ids)
            evidence.extend(self._copy_evidence(memory_id, candidate_ids, candidate_map, evidence_keys, now))

        removed_required = {"memory_id", "candidate_ids"}
        for value in raw["removed"]:  # type: ignore[index]
            if not isinstance(value, Mapping) or set(value) != removed_required:
                raise MemoryModelOutputError("Each removed decision must contain memory_id and candidate_ids.")
            memory_id = value.get("memory_id")
            if not isinstance(memory_id, str) or memory_id not in existing_map:
                raise MemoryModelOutputError("Removed decision references an unknown long-term memory.")
            if memory_id in removed_ids:
                raise MemoryModelOutputError("Removed memory ids must be unique.")
            candidate_ids = _candidate_ids(value.get("candidate_ids"), candidate_map)
            removed_ids.append(memory_id)
            selected_ids.update(candidate_ids)
            evidence.extend(self._copy_evidence(memory_id, candidate_ids, candidate_map, evidence_keys, now))

        if set(retained_ids) & set(removed_ids):
            raise MemoryModelOutputError("A memory cannot be retained and removed together.")
        if any(candidate_id not in candidate_map for candidate_id in rejected_ids):
            raise MemoryModelOutputError("Rejected decision references an unknown candidate.")
        if selected_ids & set(rejected_ids):
            raise MemoryModelOutputError("A candidate cannot be selected and rejected together.")
        if selected_ids | set(rejected_ids) != set(candidate_map):
            raise MemoryModelOutputError("Every pending candidate must be selected or rejected.")

        selection = MemorySelectionDiff(tuple(added), tuple(retained_ids), tuple(removed_ids))
        return _ParsedConsolidation(
            selection,
            tuple(evidence),
            tuple(sorted(selected_ids)),
            tuple(rejected_ids),
        )

    def _parse_added(
        self,
        value: object,
        request: MemoryConsolidationRequest,
        candidate_map: Mapping[str, EpisodicMemoryRecord],
        existing_items: Sequence[MemoryItem],
        now: str,
    ) -> tuple[MemoryItem, tuple[str, ...]]:
        required = {
            "kind",
            "title",
            "content",
            "summary",
            "scope",
            "project_id",
            "confidence",
            "tags",
            "candidate_ids",
        }
        if not isinstance(value, Mapping) or set(value) != required:
            raise MemoryModelOutputError("Each added memory must match the strict schema.")
        try:
            kind = MemoryKind(str(value.get("kind")))
            scope = MemoryScope(str(value.get("scope")))
        except ValueError as exc:
            raise MemoryModelOutputError("Added memory kind or scope is invalid.") from exc
        if kind not in {MemoryKind.SEMANTIC, MemoryKind.PROCEDURAL}:
            raise MemoryModelOutputError("Phase 2 may add only semantic or procedural memories.")
        candidate_ids = _candidate_ids(value.get("candidate_ids"), candidate_map)
        expected_project = request.project_id
        project_id = value.get("project_id")
        if project_id is not None and not isinstance(project_id, str):
            raise MemoryModelOutputError("Added memory project_id must be a string or null.")
        expected_scope = MemoryScope.PROJECT if expected_project else MemoryScope.GLOBAL
        if scope is not expected_scope or project_id != expected_project:
            raise MemoryModelOutputError("Phase-2 output cannot widen or change candidate scope.")
        confidence = value.get("confidence")
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= float(confidence) <= 1:
            raise MemoryModelOutputError("Added memory confidence must be between 0 and 1.")
        tags = value.get("tags")
        if (
            not isinstance(tags, list)
            or len(tags) > 32
            or any(not isinstance(tag, str) for tag in tags)
            or len(set(tags)) != len(tags)
        ):
            raise MemoryModelOutputError("Added memory tags are invalid.")
        title = _clean_text(value.get("title"), "title", 500, required=True)
        content = _clean_text(value.get("content"), "content", 64 * 1024, required=True)
        summary = _clean_text(value.get("summary"), "summary", 8 * 1024, required=False)
        clean_tags = tuple(_clean_text(tag, "tag", 80, required=True) for tag in tags)
        if len(set(clean_tags)) != len(clean_tags):
            raise MemoryModelOutputError("Added memory tags must remain unique after sanitization.")
        signature = (kind.value, scope.value, project_id, _normalize(content))
        if any(
            (item.kind.value, item.scope.value, item.project_id, _normalize(item.content)) == signature
            for item in existing_items
        ):
            raise MemoryModelOutputError("Exact existing memories must be retained instead of added again.")
        memory_id = f"memory_{_digest(kind.value, scope.value, project_id or '', content, *candidate_ids)[:32]}"
        return (
            MemoryItem(
                memory_id=memory_id,
                kind=kind,
                title=title,
                content=content,
                summary=summary,
                scope=scope,
                project_id=project_id,
                confidence=float(confidence),
                tags=clean_tags,
                created_at=now,
                updated_at=now,
            ),
            candidate_ids,
        )

    @staticmethod
    def _copy_evidence(
        target_memory_id: str,
        candidate_ids: Sequence[str],
        candidate_map: Mapping[str, EpisodicMemoryRecord],
        seen: set[tuple[str, str, str | None, str]],
        now: str,
    ) -> list[MemoryEvidence]:
        copied: list[MemoryEvidence] = []
        for candidate_id in candidate_ids:
            for source in candidate_map[candidate_id].evidence:
                key = (target_memory_id, source.session_id, source.turn_id, source.content_sha256)
                if key in seen:
                    continue
                seen.add(key)
                copied.append(
                    MemoryEvidence(
                        evidence_id=f"evidence_{_digest(target_memory_id, source.evidence_id)[:32]}",
                        memory_id=target_memory_id,
                        session_id=source.session_id,
                        turn_id=source.turn_id,
                        excerpt=source.excerpt,
                        source_kind="consolidated_conversation",
                        content_sha256=source.content_sha256,
                        created_at=now,
                    )
                )
        return copied


@dataclass(frozen=True)
class _ParsedConsolidation:
    selection: MemorySelectionDiff
    evidence: tuple[MemoryEvidence, ...]
    selected_candidate_ids: tuple[str, ...]
    rejected_candidate_ids: tuple[str, ...]


def _existing_view(item: MemoryItem) -> ExistingMemoryView:
    return ExistingMemoryView(
        item.memory_id,
        item.kind,
        item.title,
        item.content,
        item.summary,
        item.scope,
        item.project_id,
        item.confidence,
        item.tags,
    )


def _candidate_ids(value: object, candidates: Mapping[str, EpisodicMemoryRecord]) -> tuple[str, ...]:
    ids = _unique_string_list(value, "candidate_ids")
    if not ids or any(candidate_id not in candidates for candidate_id in ids):
        raise MemoryModelOutputError("Decision candidate_ids are invalid.")
    return ids


def _unique_string_list(value: object, name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise MemoryModelOutputError(f"{name} must be an array of strings.")
    values = tuple(value)
    if len(set(values)) != len(values):
        raise MemoryModelOutputError(f"{name} must not contain duplicates.")
    return values


def _clean_text(value: object, name: str, max_bytes: int, *, required: bool) -> str:
    if not isinstance(value, str):
        raise MemoryModelOutputError(f"Long-term memory {name} must be a string.")
    result = MemorySanitizer(max_bytes=max_bytes).sanitize(value)
    if required and not result.text:
        raise MemoryModelOutputError(f"Long-term memory {name} must not be empty after sanitization.")
    return result.text


def _normalize(value: str) -> str:
    return " ".join(value.casefold().split())


def _digest(*values: str) -> str:
    return hashlib.sha256("\x1f".join(values).encode("utf-8")).hexdigest()


__all__ = [
    "MEMORY_CONSOLIDATION_SCHEMA",
    "ConsolidationCandidateView",
    "ExistingMemoryView",
    "ManualMemoryConsolidator",
    "MemoryConsolidationModel",
    "MemoryConsolidationRequest",
    "MemoryConsolidationResult",
    "MemoryConsolidationStore",
    "MemoryConsolidator",
    "MemoryPhase2Store",
]
