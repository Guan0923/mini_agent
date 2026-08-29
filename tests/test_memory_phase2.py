"""Manual Phase-2 consolidation, evidence, conflict, and forgetting tests."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from backend.configuration import ClientPaths
from backend.domain.memory import (
    EpisodicMemoryRecord,
    MemoryCandidate,
    MemoryCandidateStatus,
    MemoryEvidence,
    MemoryItem,
    MemoryKind,
    MemoryScope,
    MemoryStatus,
    MemoryWatermark,
)
from backend.runtime.memory import ManualMemoryConsolidator, MemoryModelOutputError
from backend.storage.memory import MemoryStore

T0 = "2026-01-01T00:00:00+00:00"
T1 = "2026-01-01T00:00:01+00:00"


def _episodic(session_id: str, suffix: str, *, project_id: str | None = None) -> EpisodicMemoryRecord:
    memory_id = f"memory_episode_{suffix}"
    candidate_id = f"candidate_episode_{suffix}"
    excerpt = f"User evidence from {session_id} for {suffix}."
    item = MemoryItem(
        memory_id=memory_id,
        kind=MemoryKind.EPISODIC,
        title=f"Episode {suffix}",
        content=f"Durable candidate {suffix}",
        scope=MemoryScope.PROJECT if project_id else MemoryScope.GLOBAL,
        project_id=project_id,
        created_at=T0,
        updated_at=T0,
    )
    candidate = MemoryCandidate(
        candidate_id=candidate_id,
        kind=MemoryKind.EPISODIC,
        content=item.content,
        session_id=session_id,
        project_id=project_id,
        memory_id=memory_id,
        created_at=T0,
        updated_at=T0,
    )
    evidence = MemoryEvidence(
        evidence_id=f"evidence_episode_{suffix}",
        memory_id=memory_id,
        session_id=session_id,
        turn_id=f"turn_{suffix}",
        excerpt=excerpt,
        content_sha256=hashlib.sha256(excerpt.encode()).hexdigest(),
        created_at=T0,
    )
    return EpisodicMemoryRecord(candidate, item, (evidence,))


def _record(store: MemoryStore, record: EpisodicMemoryRecord, position: int = 1) -> None:
    store.record_phase1_batch(
        (record,), MemoryWatermark(record.candidate.session_id, position, f"event_{position}", T0)
    )


class _AddingModel:
    def __init__(self, *, add_procedural: bool = True) -> None:
        self.add_procedural = add_procedural
        self.requests = []

    def consolidate_memories(self, request):
        self.requests.append(request)
        candidate_ids = [candidate.candidate_id for candidate in request.candidates]
        added = [
            {
                "kind": "semantic",
                "title": "Stable preference",
                "content": "The user prefers concise reports.",
                "summary": "Concise reports",
                "scope": "project" if request.project_id else "global",
                "project_id": request.project_id,
                "confidence": 0.95,
                "tags": ["preference"],
                "candidate_ids": candidate_ids,
            }
        ]
        if self.add_procedural:
            added.append(
                {
                    "kind": "procedural",
                    "title": "Report workflow",
                    "content": (
                        "Suggestion: draft the summary before adding implementation details. token=phase2-secret-value"
                    ),
                    "summary": "Draft summary first",
                    "scope": "project" if request.project_id else "global",
                    "project_id": request.project_id,
                    "confidence": 0.8,
                    "tags": ["workflow", "suggestion"],
                    "candidate_ids": candidate_ids,
                }
            )
        return {"added": added, "retained": [], "removed": [], "rejected_candidate_ids": []}


def test_phase2_consolidates_across_sessions_with_direct_evidence_and_projection(tmp_path: Path) -> None:
    store = MemoryStore(ClientPaths(tmp_path / "user"))
    first = _episodic("session_a", "a", project_id="project_a")
    second = _episodic("session_b", "b", project_id="project_a")
    _record(store, first)
    _record(store, second)
    model = _AddingModel()

    result = ManualMemoryConsolidator(store, model, clock=lambda: T1).consolidate(project_id="project_a")

    assert result.model_called
    assert {item.kind for item in result.selection.added} == {MemoryKind.SEMANTIC, MemoryKind.PROCEDURAL}
    assert all(
        store.get_candidate(record.candidate.candidate_id).status is MemoryCandidateStatus.SELECTED
        for record in (first, second)
    )  # type: ignore[union-attr]
    for item in result.selection.added:
        evidence = store.list_evidence(memory_id=item.memory_id)
        assert {source.session_id for source in evidence} == {"session_a", "session_b"}
        assert all(source.source_kind == "consolidated_conversation" for source in evidence)
    assert result.raw_projection is not None
    projection = result.raw_projection.read_text(encoding="utf-8")
    assert "The user prefers concise reports." in projection
    assert "Suggestion: draft the summary" in projection
    assert "phase2-secret-value" not in projection

    replay = ManualMemoryConsolidator(store, model, clock=lambda: T1).consolidate(project_id="project_a")
    assert not replay.model_called
    assert len(model.requests) == 1


def test_phase2_new_evidence_soft_deletes_conflict_and_can_restore(tmp_path: Path) -> None:
    store = MemoryStore(ClientPaths(tmp_path / "user"))
    old = MemoryItem(
        memory_id="memory_old_fact",
        kind=MemoryKind.SEMANTIC,
        title="Old preference",
        content="The user prefers detailed reports.",
        created_at=T0,
        updated_at=T0,
    )
    store.create_item(old)
    episode = _episodic("session_new", "new")
    _record(store, episode)

    class CorrectingModel:
        @staticmethod
        def consolidate_memories(request):
            candidate_id = request.candidates[0].candidate_id
            return {
                "added": [
                    {
                        "kind": "semantic",
                        "title": "Current preference",
                        "content": "The user now prefers concise reports.",
                        "summary": "Updated preference",
                        "scope": "global",
                        "project_id": None,
                        "confidence": 0.95,
                        "tags": ["preference"],
                        "candidate_ids": [candidate_id],
                    }
                ],
                "retained": [],
                "removed": [
                    {
                        "memory_id": "memory_old_fact",
                        "candidate_ids": [candidate_id],
                    }
                ],
                "rejected_candidate_ids": [],
            }

    result = ManualMemoryConsolidator(store, CorrectingModel(), clock=lambda: T1).consolidate()
    deleted = store.get_item(old.memory_id, include_deleted=True)
    assert deleted is not None and deleted.status is MemoryStatus.DELETED
    assert store.list_evidence(memory_id=old.memory_id)[0].session_id == "session_new"
    corrected = result.selection.added[0]
    assert store.list_evidence(memory_id=corrected.memory_id)[0].session_id == "session_new"
    restored = store.restore_item(old.memory_id, restored_at=T1)
    assert restored.status is MemoryStatus.ACTIVE and restored.deleted_at is None
    assert store.search_items("detailed")[0].item.memory_id == old.memory_id


def test_phase2_retains_existing_and_adds_new_evidence(tmp_path: Path) -> None:
    store = MemoryStore(ClientPaths(tmp_path / "user"))
    existing = MemoryItem(
        memory_id="memory_existing",
        kind=MemoryKind.SEMANTIC,
        title="Existing fact",
        content="The user prefers concise reports.",
        created_at=T0,
        updated_at=T0,
    )
    store.create_item(existing)
    episode = _episodic("session_support", "support")
    _record(store, episode)

    class RetainingModel:
        @staticmethod
        def consolidate_memories(request):
            return {
                "added": [],
                "retained": [
                    {
                        "memory_id": "memory_existing",
                        "candidate_ids": [request.candidates[0].candidate_id],
                    }
                ],
                "removed": [],
                "rejected_candidate_ids": [],
            }

    result = ManualMemoryConsolidator(store, RetainingModel(), clock=lambda: T1).consolidate()
    assert result.selection.retained_ids == (existing.memory_id,)
    assert store.list_evidence(memory_id=existing.memory_id)[0].session_id == "session_support"


def test_phase2_schema_failure_leaves_all_candidates_pending(tmp_path: Path) -> None:
    store = MemoryStore(ClientPaths(tmp_path / "user"))
    first = _episodic("session_a", "a")
    second = _episodic("session_b", "b")
    _record(store, first)
    _record(store, second)

    class IncompleteModel:
        @staticmethod
        def consolidate_memories(request):
            return {
                "added": [],
                "retained": [],
                "removed": [],
                "rejected_candidate_ids": [request.candidates[0].candidate_id],
            }

    with pytest.raises(MemoryModelOutputError, match="Every pending candidate"):
        ManualMemoryConsolidator(store, IncompleteModel(), clock=lambda: T1).consolidate()
    assert all(
        store.get_candidate(record.candidate.candidate_id).status is MemoryCandidateStatus.PENDING  # type: ignore[union-attr]
        for record in (first, second)
    )
    assert store.list_items(kinds=(MemoryKind.SEMANTIC, MemoryKind.PROCEDURAL)) == []


def test_phase2_keeps_project_candidate_sets_isolated(tmp_path: Path) -> None:
    store = MemoryStore(ClientPaths(tmp_path / "user"))
    global_record = _episodic("session_global", "global")
    project_record = _episodic("session_project", "project", project_id="project_a")
    _record(store, global_record)
    _record(store, project_record)
    model = _AddingModel(add_procedural=False)

    ManualMemoryConsolidator(store, model, clock=lambda: T1).consolidate(project_id="project_a")
    assert store.get_candidate(project_record.candidate.candidate_id).status is MemoryCandidateStatus.SELECTED  # type: ignore[union-attr]
    assert store.get_candidate(global_record.candidate.candidate_id).status is MemoryCandidateStatus.PENDING  # type: ignore[union-attr]
    assert all(candidate.project_id == "project_a" for candidate in model.requests[0].candidates)
