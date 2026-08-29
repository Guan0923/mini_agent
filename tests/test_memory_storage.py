"""SQLite, projection, isolation, migration, and path tests for memory storage."""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from backend.configuration import ClientPaths
from backend.domain.memory import (
    MemoryCandidate,
    MemoryCandidateStatus,
    MemoryEvidence,
    MemoryItem,
    MemoryJob,
    MemoryJobKind,
    MemoryJobStatus,
    MemoryKind,
    MemoryScope,
    MemoryStatus,
    MemoryWatermark,
)
from backend.runtime.agent_init import initialize_project_agents
from backend.runtime.instructions import discover_agent_instructions
from backend.storage.memory import (
    MEMORY_SCHEMA_VERSION,
    MemoryConflictError,
    MemorySchemaError,
    MemoryStorageError,
    MemoryStore,
)

T0 = "2026-01-01T00:00:00+00:00"
T1 = "2026-01-01T00:00:01+00:00"
T2 = "2026-01-01T00:00:02+00:00"


def _item(
    memory_id: str,
    kind: MemoryKind = MemoryKind.SEMANTIC,
    *,
    content: str = "fixed searchable content",
    project_id: str | None = None,
) -> MemoryItem:
    return MemoryItem(
        memory_id=memory_id,
        kind=kind,
        title=f"Title {memory_id}",
        content=content,
        scope=MemoryScope.PROJECT if project_id else MemoryScope.GLOBAL,
        project_id=project_id,
        tags=("fixed-tag",),
        created_at=T0,
        updated_at=T0,
    )


def _candidate(candidate_id: str, source_id: str = "session_a") -> MemoryCandidate:
    return MemoryCandidate(
        candidate_id=candidate_id,
        kind=MemoryKind.EPISODIC,
        content=f"candidate {candidate_id}",
        session_id=source_id,
        created_at=T0,
        updated_at=T0,
    )


def test_memory_paths_are_lazy_and_unrelated_agents_operations_do_not_create_them(tmp_path: Path) -> None:
    paths = ClientPaths(tmp_path / "user_a")
    store = MemoryStore(paths)
    assert store.get_item("memory_absent") is None
    assert store.list_items() == []
    assert not paths.memories_dir.exists()

    paths.ensure()
    global_root = paths.root
    project_root = tmp_path / "project"
    project_root.mkdir()
    (global_root / "AGENTS.md").write_text("fixed global", encoding="utf-8")
    discover_agent_instructions(global_root=global_root, project_root=project_root)
    initialize_project_agents(project_root)
    assert not paths.memories_dir.exists()

    store.ensure()
    assert paths.memory_db.is_file()
    assert paths.rollout_summaries_dir.is_dir()
    assert not paths.raw_memories_file.exists()
    assert store.schema_version() == MEMORY_SCHEMA_VERSION

    write_paths = ClientPaths(tmp_path / "user_first_write")
    MemoryStore(write_paths).create_item(_item("memory_first_write"))
    assert write_paths.memory_db.is_file()


def test_item_crud_fts_scope_and_user_isolation(tmp_path: Path) -> None:
    first = MemoryStore(ClientPaths(tmp_path / "user_a"))
    second = MemoryStore(ClientPaths(tmp_path / "user_b"))
    episodic = _item("memory_episode", MemoryKind.EPISODIC, content="visited Kyoto station")
    semantic = _item("memory_semantic", content="Python uses indentation")
    procedure = _item(
        "memory_procedure",
        MemoryKind.PROCEDURAL,
        content="run uv sync first",
        project_id="project_a",
    )
    hidden = _item("memory_hidden", content="private alpha", project_id="project_b")
    for item in (episodic, semantic, procedure, hidden):
        first.create_item(item)

    assert {item.memory_id for item in first.list_items()} == {"memory_episode", "memory_semantic"}
    assert {item.memory_id for item in first.list_items(project_id="project_a")} == {
        "memory_episode",
        "memory_semantic",
        "memory_procedure",
    }
    assert {item.memory_id for item in first.list_accessible_items(["project_a"])} == {
        "memory_episode",
        "memory_semantic",
        "memory_procedure",
    }
    assert "memory_hidden" not in {
        item.memory_id for item in first.list_accessible_items(["project_a"], include_deleted=True)
    }
    assert first.search_items("private", project_id="project_a") == []
    assert [result.item.memory_id for result in first.search_items("indentation")] == ["memory_semantic"]
    first.add_evidence(
        MemoryEvidence(
            evidence_id="evidence_owner_only",
            memory_id="memory_semantic",
            session_id="session_owner",
            excerpt="owner-only evidence",
            created_at=T0,
        )
    )
    first.add_candidate(_candidate("candidate_owner_only", "session_owner"))
    first.advance_watermark(MemoryWatermark("session_owner", 1, "event_owner", T0))
    first.enqueue_job(
        MemoryJob(
            job_id="memory_job_owner_only",
            kind=MemoryJobKind.EXTRACT,
            source_id="session_owner",
            available_at=T0,
            created_at=T0,
            updated_at=T0,
        )
    )
    assert second.get_item("memory_semantic") is None
    assert second.list_evidence(session_id="session_owner") == []
    assert second.get_candidate("candidate_owner_only") is None
    assert second.get_watermark("session_owner") is None
    assert second.list_jobs() == []
    assert not second.paths.memories_dir.exists()

    updated = replace(semantic, content="Python uses significant whitespace", updated_at=T1)
    first.update_item(updated)
    assert first.search_items("indentation") == []
    assert [result.item.memory_id for result in first.search_items("whitespace")] == ["memory_semantic"]
    deleted = first.delete_item("memory_semantic", deleted_at=T2)
    assert deleted.status is MemoryStatus.DELETED
    assert first.get_item("memory_semantic") is None
    assert first.get_item("memory_semantic", include_deleted=True) == deleted
    assert first.search_items("whitespace") == []
    with pytest.raises(MemoryConflictError, match="cannot be restored"):
        first.update_item(replace(updated, updated_at="2026-01-01T00:00:03+00:00"))
    with pytest.raises(MemoryConflictError, match="created_at is immutable"):
        first.update_item(replace(episodic, created_at=T1, updated_at=T1))


def test_evidence_deduplicates_null_turn_and_cascades_on_purge(tmp_path: Path) -> None:
    store = MemoryStore(ClientPaths(tmp_path / "user"))
    store.create_item(_item("memory_a"))
    evidence = MemoryEvidence(
        evidence_id="evidence_a",
        memory_id="memory_a",
        session_id="session_a",
        excerpt="fixed source excerpt",
        created_at=T0,
    )
    stored = store.add_evidence(evidence)
    assert len(stored.content_sha256) == 64
    with pytest.raises(MemoryConflictError):
        store.add_evidence(replace(evidence, evidence_id="evidence_b"))
    assert store.list_evidence(memory_id="memory_a") == [stored]
    store.purge_item("memory_a")
    assert store.list_evidence(session_id="session_a") == []


def test_candidate_transitions_and_atomic_watermark_batches(tmp_path: Path) -> None:
    store = MemoryStore(ClientPaths(tmp_path / "user"))
    decided = store.add_candidate(_candidate("candidate_decided"))
    decided = store.set_candidate_status(decided.candidate_id, MemoryCandidateStatus.SELECTED, updated_at=T1)
    assert decided.status is MemoryCandidateStatus.SELECTED
    with pytest.raises(MemoryConflictError, match="cannot change"):
        store.set_candidate_status(decided.candidate_id, MemoryCandidateStatus.REJECTED, updated_at=T2)

    watermark = MemoryWatermark("session_a", 4, "event_4", T1)
    store.record_extraction_batch((_candidate("candidate_batch"),), watermark)
    assert store.get_watermark("session_a") == watermark
    with pytest.raises(MemoryConflictError, match="backwards"):
        store.advance_watermark(MemoryWatermark("session_a", 3, "event_3", T2))

    with pytest.raises(MemoryConflictError):
        store.record_extraction_batch(
            (_candidate("candidate_rolled_back"), _candidate("candidate_batch")),
            MemoryWatermark("session_a", 8, "event_8", T2),
        )
    assert store.get_candidate("candidate_rolled_back") is None
    assert store.get_watermark("session_a") == watermark


def test_jobs_use_leases_retry_limits_and_expiry_recovery(tmp_path: Path) -> None:
    store = MemoryStore(ClientPaths(tmp_path / "user"))
    job = MemoryJob(
        job_id="memory_job_a",
        kind=MemoryJobKind.EXTRACT,
        max_attempts=2,
        available_at=T0,
        created_at=T0,
        updated_at=T0,
    )
    store.enqueue_job(job)
    first = store.claim_job("worker_a", now=T0, lease_seconds=1)
    assert first is not None and first.attempts == 1
    assert store.claim_job("worker_b", now=T0) is None
    second = store.claim_job("worker_b", now=T1, lease_seconds=1)
    assert second is not None and second.attempts == 2
    failed = store.fail_job("memory_job_a", "worker_b", "fixed failure", failed_at=T1)
    assert failed.status is MemoryJobStatus.FAILED
    assert failed.lease_owner is None

    abandoned = replace(job, job_id="memory_job_abandoned", max_attempts=1)
    store.enqueue_job(abandoned)
    assert store.claim_job("worker_a", now=T0, lease_seconds=1) is not None
    assert store.claim_job("worker_b", now=T1) is None
    assert store.get_job(abandoned.job_id).status is MemoryJobStatus.FAILED  # type: ignore[union-attr]

    completed_job = replace(job, job_id="memory_job_complete")
    store.enqueue_job(completed_job)
    store.claim_job("worker_a", now=T0)
    with pytest.raises(MemoryConflictError):
        store.complete_job(completed_job.job_id, "worker_b", completed_at=T1)
    assert store.complete_job(completed_job.job_id, "worker_a", completed_at=T1).status is MemoryJobStatus.SUCCEEDED


def test_jobs_are_idempotent_cancelable_and_clear_is_transactional(tmp_path: Path) -> None:
    store = MemoryStore(ClientPaths(tmp_path / "user"))
    first = MemoryJob(
        job_id="memory_job_first",
        kind=MemoryJobKind.EXTRACT,
        source_id="session_a",
        available_at=T0,
        created_at=T0,
        updated_at=T0,
    )
    duplicate = replace(first, job_id="memory_job_duplicate")
    assert store.enqueue_job_if_absent(first) == (first, True)
    assert store.enqueue_job_if_absent(duplicate) == (first, False)

    claimed = store.claim_job("worker_a", now=T0)
    assert claimed is not None
    cancelled = store.cancel_job(first.job_id, cancelled_at=T1, reason="fixed_reason")
    assert cancelled.status is MemoryJobStatus.CANCELLED
    assert cancelled.lease_owner is None

    store.create_item(_item("memory_clear"))
    store.advance_watermark(MemoryWatermark("session_a", 1, "event_1", T1))
    store.clear_all()
    assert store.list_items(include_deleted=True) == []
    assert store.list_jobs() == []
    assert store.get_watermark("session_a") is None
    assert store.schema_version() == MEMORY_SCHEMA_VERSION


def test_disabled_item_is_hidden_from_fts_and_can_be_reenabled(tmp_path: Path) -> None:
    store = MemoryStore(ClientPaths(tmp_path / "user"))
    store.create_item(_item("memory_toggle", content="durable zebra preference"))
    assert store.search_items("zebra")
    disabled = store.set_item_enabled("memory_toggle", enabled=False, changed_at=T1)
    assert disabled.status is MemoryStatus.DISABLED
    assert store.search_items("zebra") == []
    enabled = store.set_item_enabled("memory_toggle", enabled=True, changed_at=T2)
    assert enabled.status is MemoryStatus.ACTIVE
    assert store.search_items("zebra")


def test_schema_migration_and_validation_guards(tmp_path: Path) -> None:
    empty_paths = ClientPaths(tmp_path / "empty")
    empty_paths.memories_dir.mkdir(parents=True)
    sqlite3.connect(empty_paths.memory_db).close()
    MemoryStore(empty_paths).ensure()
    assert MemoryStore(empty_paths).schema_version() == MEMORY_SCHEMA_VERSION

    future_paths = ClientPaths(tmp_path / "future")
    future_paths.memories_dir.mkdir(parents=True)
    with sqlite3.connect(future_paths.memory_db) as connection:
        connection.execute("PRAGMA user_version=99")
    with pytest.raises(MemorySchemaError, match="newer"):
        MemoryStore(future_paths).ensure()

    legacy_paths = ClientPaths(tmp_path / "legacy")
    legacy_paths.memories_dir.mkdir(parents=True)
    with sqlite3.connect(legacy_paths.memory_db) as connection:
        connection.execute("CREATE TABLE unknown(value TEXT)")
    with pytest.raises(MemorySchemaError, match="Unversioned"):
        MemoryStore(legacy_paths).ensure()

    damaged_paths = ClientPaths(tmp_path / "damaged")
    MemoryStore(damaged_paths).ensure()
    with sqlite3.connect(damaged_paths.memory_db) as connection:
        connection.execute("DROP TRIGGER memory_items_fts_update")
    with pytest.raises(MemorySchemaError, match="triggers"):
        MemoryStore(damaged_paths).ensure()


def test_schema_migrates_v1_candidates_to_v2_links(tmp_path: Path) -> None:
    paths = ClientPaths(tmp_path / "v1")
    store = MemoryStore(paths)
    store.ensure()
    store.add_candidate(_candidate("candidate_legacy"))
    with sqlite3.connect(paths.memory_db) as connection:
        connection.executescript(
            """
            DROP INDEX memory_candidates_memory_idx;
            ALTER TABLE memory_candidates DROP COLUMN memory_id;
            ALTER TABLE memory_metadata RENAME TO memory_metadata_v2;
            CREATE TABLE memory_metadata (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                schema_version INTEGER NOT NULL CHECK (schema_version = 1),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            INSERT INTO memory_metadata(singleton,schema_version,created_at,updated_at)
                SELECT singleton,1,created_at,updated_at FROM memory_metadata_v2;
            DROP TABLE memory_metadata_v2;
            PRAGMA user_version = 1;
            """
        )

    migrated = MemoryStore(paths)
    migrated.ensure()
    assert migrated.schema_version() == MEMORY_SCHEMA_VERSION
    assert migrated.get_candidate("candidate_legacy").memory_id is None  # type: ignore[union-attr]
    with sqlite3.connect(paths.memory_db) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    with sqlite3.connect(paths.memory_db) as connection:
        columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(memory_candidates)")}
    assert "memory_id" in columns


def test_projections_are_rebuilt_from_database_and_include_project_items(tmp_path: Path) -> None:
    store = MemoryStore(ClientPaths(tmp_path / "user"))
    global_item = _item("memory_global", content="global durable fact")
    project_item = _item("memory_project", content="project durable procedure", project_id="project_a")
    store.create_item(global_item)
    store.create_item(project_item)
    store.add_evidence(
        MemoryEvidence(
            evidence_id="evidence_project",
            memory_id=project_item.memory_id,
            session_id="session_a",
            excerpt="fixed project evidence",
            created_at=T0,
        )
    )
    stale = store.paths.rollout_summaries_dir / "stale.md"
    stale.write_text("stale", encoding="utf-8")
    raw, rollouts = store.rebuild_projections()
    assert "global durable fact" in raw.read_text(encoding="utf-8")
    assert "project durable procedure" in raw.read_text(encoding="utf-8")
    assert len(rollouts) == 1
    assert "fixed project evidence" in rollouts[0].read_text(encoding="utf-8")
    assert not stale.exists()

    raw.unlink()
    rollouts[0].unlink()
    rebuilt_raw, rebuilt_rollouts = store.rebuild_projections()
    assert rebuilt_raw.is_file()
    assert rebuilt_rollouts[0].is_file()


@pytest.mark.parametrize("bad_child", ["memory.db", "raw_memories.md", "rollout_summaries"])
def test_memory_child_paths_reject_wrong_file_types(tmp_path: Path, bad_child: str) -> None:
    paths = ClientPaths(tmp_path / bad_child.replace(".", "_"))
    paths.memories_dir.mkdir(parents=True)
    target = paths.memories_dir / bad_child
    if bad_child == "rollout_summaries":
        target.write_text("not a directory", encoding="utf-8")
    else:
        target.mkdir()
    with pytest.raises(MemoryStorageError):
        MemoryStore(paths).ensure()


def test_memories_root_rejects_regular_file(tmp_path: Path) -> None:
    paths = ClientPaths(tmp_path / "user_file")
    paths.root.mkdir()
    paths.memories_dir.write_text("not a directory", encoding="utf-8")
    with pytest.raises(MemoryStorageError):
        MemoryStore(paths).ensure()


@pytest.mark.parametrize("linked_child", ["memories", "memory.db", "raw_memories.md", "rollout_summaries"])
def test_memory_paths_reject_symbolic_links(tmp_path: Path, linked_child: str) -> None:
    paths = ClientPaths(tmp_path / f"user_{linked_child.replace('.', '_')}")
    paths.root.mkdir()
    if linked_child != "memories":
        paths.memories_dir.mkdir()
    outside = tmp_path / f"outside_{linked_child.replace('.', '_')}"
    target = paths.memories_dir if linked_child == "memories" else paths.memories_dir / linked_child
    is_directory = linked_child in {"memories", "rollout_summaries"}
    if is_directory:
        outside.mkdir()
    else:
        outside.write_text("outside", encoding="utf-8")
    try:
        target.symlink_to(outside, target_is_directory=is_directory)
    except OSError:
        pytest.skip("Symbolic links are unavailable for this Windows test account.")
    with pytest.raises(MemoryStorageError, match="symbolic link"):
        MemoryStore(paths).ensure()
