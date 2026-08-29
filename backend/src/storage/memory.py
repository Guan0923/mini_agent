"""Authoritative SQLite storage and rebuildable projections for local memory."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from itertools import islice
from pathlib import Path
from uuid import uuid4

from backend.configuration import ClientPaths, ConfigurationError
from backend.domain.memory import (
    EpisodicMemoryRecord,
    MemoryCandidate,
    MemoryCandidateStatus,
    MemoryEvidence,
    MemoryItem,
    MemoryJob,
    MemoryJobKind,
    MemoryJobStatus,
    MemoryKind,
    MemoryScope,
    MemorySearchResult,
    MemorySelectionDiff,
    MemoryStatus,
    MemoryWatermark,
)
from backend.domain.state import utc_now

MEMORY_SCHEMA_VERSION = 3

MEMORY_SCHEMA = f"""
PRAGMA foreign_keys = ON;
CREATE TABLE memory_metadata (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    schema_version INTEGER NOT NULL CHECK (schema_version = {MEMORY_SCHEMA_VERSION}),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE memory_items (
    memory_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('episodic','semantic','procedural')),
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    scope TEXT NOT NULL CHECK (scope IN ('global','project')),
    project_id TEXT,
    confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    tags_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL CHECK (status IN ('active','disabled','superseded','deleted')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_used_at TEXT,
    deleted_at TEXT,
    CHECK ((scope = 'global' AND project_id IS NULL) OR (scope = 'project' AND project_id IS NOT NULL)),
    CHECK ((status = 'deleted' AND deleted_at IS NOT NULL) OR (status != 'deleted' AND deleted_at IS NULL))
);
CREATE INDEX memory_items_scope_idx ON memory_items(scope,project_id,status,updated_at);
CREATE INDEX memory_items_kind_idx ON memory_items(kind,status,updated_at);
CREATE TABLE memory_evidence (
    evidence_id TEXT PRIMARY KEY,
    memory_id TEXT NOT NULL REFERENCES memory_items(memory_id) ON DELETE CASCADE,
    session_id TEXT NOT NULL,
    turn_id TEXT,
    excerpt TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX memory_evidence_memory_idx ON memory_evidence(memory_id,created_at);
CREATE INDEX memory_evidence_session_idx ON memory_evidence(session_id,created_at);
CREATE UNIQUE INDEX memory_evidence_unique_idx
ON memory_evidence(memory_id,session_id,IFNULL(turn_id,''),content_sha256);
CREATE TABLE memory_candidates (
    candidate_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('episodic','semantic','procedural')),
    content TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    session_id TEXT NOT NULL,
    turn_id TEXT,
    project_id TEXT,
    memory_id TEXT REFERENCES memory_items(memory_id) ON DELETE SET NULL,
    confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    status TEXT NOT NULL CHECK (status IN ('pending','selected','rejected')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX memory_candidates_status_idx ON memory_candidates(status,created_at,candidate_id);
CREATE INDEX memory_candidates_session_idx ON memory_candidates(session_id,created_at,candidate_id);
CREATE UNIQUE INDEX memory_candidates_memory_idx ON memory_candidates(memory_id) WHERE memory_id IS NOT NULL;
CREATE TABLE memory_watermarks (
    source_id TEXT PRIMARY KEY,
    position INTEGER NOT NULL CHECK (position >= 0),
    event_id TEXT,
    updated_at TEXT NOT NULL
);
CREATE TABLE memory_jobs (
    job_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('extract','consolidate','rebuild_projections')),
    status TEXT NOT NULL CHECK (status IN ('pending','running','succeeded','failed','cancelled')),
    source_id TEXT,
    project_id TEXT,
    attempts INTEGER NOT NULL CHECK (attempts >= 0),
    max_attempts INTEGER NOT NULL CHECK (max_attempts >= 1),
    available_at TEXT NOT NULL,
    lease_owner TEXT,
    lease_expires_at TEXT,
    last_error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (attempts <= max_attempts),
    CHECK (
        (status = 'running' AND lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)
        OR (status != 'running' AND lease_owner IS NULL AND lease_expires_at IS NULL)
    )
);
CREATE INDEX memory_jobs_claim_idx ON memory_jobs(status,available_at,lease_expires_at,created_at);
CREATE UNIQUE INDEX memory_jobs_active_source_idx
ON memory_jobs(kind,IFNULL(source_id,''),IFNULL(project_id,''))
WHERE status IN ('pending','running');
CREATE VIRTUAL TABLE memory_items_fts USING fts5(
    memory_id UNINDEXED,
    title,
    summary,
    content,
    tags,
    tokenize = 'unicode61'
);
CREATE TRIGGER memory_items_fts_insert AFTER INSERT ON memory_items
WHEN new.status = 'active'
BEGIN
    INSERT INTO memory_items_fts(memory_id,title,summary,content,tags)
    VALUES (new.memory_id,new.title,new.summary,new.content,new.tags_json);
END;
CREATE TRIGGER memory_items_fts_delete AFTER DELETE ON memory_items
BEGIN
    DELETE FROM memory_items_fts WHERE memory_id = old.memory_id;
END;
CREATE TRIGGER memory_items_fts_update AFTER UPDATE ON memory_items
BEGIN
    DELETE FROM memory_items_fts WHERE memory_id = old.memory_id;
    INSERT INTO memory_items_fts(memory_id,title,summary,content,tags)
    SELECT new.memory_id,new.title,new.summary,new.content,new.tags_json
    WHERE new.status = 'active';
END;
PRAGMA user_version = {MEMORY_SCHEMA_VERSION};
"""

_REQUIRED_TABLES = {
    "memory_metadata",
    "memory_items",
    "memory_evidence",
    "memory_candidates",
    "memory_watermarks",
    "memory_jobs",
    "memory_items_fts",
}
_REQUIRED_TRIGGERS = {
    "memory_items_fts_insert",
    "memory_items_fts_delete",
    "memory_items_fts_update",
}
_REQUIRED_INDEXES = {
    "memory_evidence_unique_idx",
    "memory_candidates_memory_idx",
    "memory_jobs_active_source_idx",
}
_QUERY_TOKEN_RE = re.compile(r"[\w-]+", re.UNICODE)


class MemoryStorageError(RuntimeError):
    """Base class for memory storage failures safe to surface to callers."""


class MemorySchemaError(MemoryStorageError):
    """The memory database schema cannot be opened safely."""


class MemoryConflictError(MemoryStorageError):
    """A unique record or lifecycle precondition was violated."""


class MemoryNotFoundError(MemoryStorageError):
    """A requested memory record does not exist."""


class MemoryStore:
    """Thread-safe per-user memory repository with lazy filesystem creation."""

    def __init__(self, paths: ClientPaths) -> None:
        self.paths = paths
        self._lock = threading.RLock()
        self._schema_ready = False

    @property
    def exists(self) -> bool:
        return self.paths.memory_db.is_file() and not self.paths.memory_db.is_symlink()

    def ensure(self) -> None:
        """Create the lazy directory contract and initialize the current schema."""

        with self._lock:
            self._ensure_schema()

    def schema_version(self) -> int | None:
        if not self.paths.memory_db.exists():
            return None
        with self._connection(create=False) as connection:
            if connection is None:
                return None
            return int(connection.execute("PRAGMA user_version").fetchone()[0])

    def create_item(self, item: MemoryItem) -> MemoryItem:
        with self._connection(create=True) as connection:
            assert connection is not None
            try:
                self._insert_item(connection, item)
            except sqlite3.IntegrityError as exc:
                raise MemoryConflictError("Memory item already exists or violates storage constraints.") from exc
        return item

    def update_item(self, item: MemoryItem) -> MemoryItem:
        with self._connection(create=True, immediate=True) as connection:
            assert connection is not None
            existing = connection.execute(
                "SELECT status,created_at FROM memory_items WHERE memory_id=?", (item.memory_id,)
            ).fetchone()
            if existing is None:
                raise MemoryNotFoundError("Memory item does not exist.")
            if str(existing["created_at"]) != item.created_at:
                raise MemoryConflictError("Memory item created_at is immutable.")
            if str(existing["status"]) == MemoryStatus.DELETED.value and item.status is not MemoryStatus.DELETED:
                raise MemoryConflictError("Deleted memory items cannot be restored in place.")
            cursor = connection.execute(
                """UPDATE memory_items SET kind=?,title=?,content=?,summary=?,scope=?,project_id=?,confidence=?,
                tags_json=?,status=?,updated_at=?,last_used_at=?,deleted_at=? WHERE memory_id=?""",
                self._item_update_values(item),
            )
            assert cursor.rowcount == 1
        return item

    def get_item(self, memory_id: str, *, include_deleted: bool = False) -> MemoryItem | None:
        with self._connection(create=False) as connection:
            if connection is None:
                return None
            query = "SELECT * FROM memory_items WHERE memory_id=?"
            values: list[object] = [memory_id]
            if not include_deleted:
                query += " AND status != 'deleted'"
            row = connection.execute(query, values).fetchone()
        return self._item_from_row(row) if row is not None else None

    def list_items(
        self,
        *,
        project_id: str | None = None,
        kinds: Sequence[MemoryKind] = (),
        include_deleted: bool = False,
        limit: int = 100,
    ) -> list[MemoryItem]:
        _validate_limit(limit)
        clauses, values = self._scope_clauses(project_id)
        if not include_deleted:
            clauses.append("status != 'deleted'")
        if kinds:
            placeholders = ",".join("?" for _ in kinds)
            clauses.append(f"kind IN ({placeholders})")
            values.extend(kind.value for kind in kinds)
        query = f"SELECT * FROM memory_items WHERE {' AND '.join(clauses)} ORDER BY updated_at DESC,memory_id LIMIT ?"
        values.append(limit)
        with self._connection(create=False) as connection:
            if connection is None:
                return []
            rows = connection.execute(query, values).fetchall()
        return [self._item_from_row(row) for row in rows]

    def list_accessible_items(
        self,
        project_ids: Sequence[str],
        *,
        kinds: Sequence[MemoryKind] = (),
        include_deleted: bool = False,
        limit: int = 100,
    ) -> list[MemoryItem]:
        """List global items plus items belonging to the supplied projects."""

        _validate_limit(limit)
        allowed_projects = set(project_ids)
        clauses: list[str] = []
        values: list[object] = []
        if not include_deleted:
            clauses.append("status != 'deleted'")
        if kinds:
            placeholders = ",".join("?" for _ in kinds)
            clauses.append(f"kind IN ({placeholders})")
            values.extend(kind.value for kind in kinds)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        query = f"SELECT * FROM memory_items{where} ORDER BY updated_at DESC,memory_id"
        with self._connection(create=False) as connection:
            if connection is None:
                return []
            rows = connection.execute(query, values).fetchall()
        items = (
            self._item_from_row(row)
            for row in rows
            if row["scope"] == MemoryScope.GLOBAL.value or row["project_id"] in allowed_projects
        )
        return list(islice(items, limit))

    def delete_item(self, memory_id: str, *, deleted_at: str | None = None) -> MemoryItem:
        now = _canonical_utc_timestamp(deleted_at, "deleted_at") if deleted_at is not None else utc_now()
        with self._connection(create=True) as connection:
            assert connection is not None
            cursor = connection.execute(
                """UPDATE memory_items SET status='deleted',deleted_at=?,updated_at=?
                WHERE memory_id=? AND status!='deleted'""",
                (now, now, memory_id),
            )
            if cursor.rowcount != 1:
                raise MemoryNotFoundError("Active memory item does not exist.")
            row = connection.execute("SELECT * FROM memory_items WHERE memory_id=?", (memory_id,)).fetchone()
        assert row is not None
        return self._item_from_row(row)

    def purge_item(self, memory_id: str) -> None:
        with self._connection(create=True) as connection:
            assert connection is not None
            pending = connection.execute(
                "SELECT 1 FROM memory_candidates WHERE memory_id=? AND status='pending' LIMIT 1",
                (memory_id,),
            ).fetchone()
            if pending is not None:
                raise MemoryConflictError("Pending episodic candidates cannot lose their memory item.")
            cursor = connection.execute("DELETE FROM memory_items WHERE memory_id=?", (memory_id,))
            if cursor.rowcount != 1:
                raise MemoryNotFoundError("Memory item does not exist.")

    def restore_item(self, memory_id: str, *, restored_at: str | None = None) -> MemoryItem:
        """Explicitly restore one soft-deleted item without weakening normal updates."""

        now = _canonical_utc_timestamp(restored_at, "restored_at") if restored_at is not None else utc_now()
        with self._connection(create=True) as connection:
            assert connection is not None
            cursor = connection.execute(
                """UPDATE memory_items SET status='active',deleted_at=NULL,updated_at=?
                WHERE memory_id=? AND status='deleted'""",
                (now, memory_id),
            )
            if cursor.rowcount != 1:
                raise MemoryNotFoundError("Deleted memory item does not exist.")
            row = connection.execute("SELECT * FROM memory_items WHERE memory_id=?", (memory_id,)).fetchone()
        assert row is not None
        return self._item_from_row(row)

    def set_item_enabled(
        self,
        memory_id: str,
        *,
        enabled: bool,
        changed_at: str | None = None,
    ) -> MemoryItem:
        """Enable or disable a non-deleted item without changing its content."""

        if not isinstance(enabled, bool):
            raise ValueError("enabled must be a boolean.")
        now = _canonical_utc_timestamp(changed_at, "changed_at") if changed_at is not None else utc_now()
        source = MemoryStatus.DISABLED.value if enabled else MemoryStatus.ACTIVE.value
        target = MemoryStatus.ACTIVE.value if enabled else MemoryStatus.DISABLED.value
        with self._connection(create=True) as connection:
            assert connection is not None
            cursor = connection.execute(
                "UPDATE memory_items SET status=?,updated_at=? WHERE memory_id=? AND status=?",
                (target, now, memory_id, source),
            )
            if cursor.rowcount != 1:
                raise MemoryNotFoundError("Memory item is not in the required lifecycle state.")
            row = connection.execute("SELECT * FROM memory_items WHERE memory_id=?", (memory_id,)).fetchone()
        assert row is not None
        return self._item_from_row(row)

    def search_items(
        self,
        query: str,
        *,
        project_id: str | None = None,
        kinds: Sequence[MemoryKind] = (),
        limit: int = 20,
    ) -> list[MemorySearchResult]:
        _validate_limit(limit)
        fts_query = _fts_query(query)
        if not fts_query:
            return []
        clauses, values = self._scope_clauses(project_id, prefix="i.")
        clauses.append("i.status='active'")
        if kinds:
            placeholders = ",".join("?" for _ in kinds)
            clauses.append(f"i.kind IN ({placeholders})")
            values.extend(kind.value for kind in kinds)
        sql = f"""SELECT i.*,bm25(memory_items_fts) AS search_rank
            FROM memory_items_fts JOIN memory_items AS i ON i.memory_id=memory_items_fts.memory_id
            WHERE memory_items_fts MATCH ? AND {" AND ".join(clauses)}
            ORDER BY search_rank,i.updated_at DESC,i.memory_id LIMIT ?"""
        with self._connection(create=False) as connection:
            if connection is None:
                return []
            try:
                rows = connection.execute(sql, [fts_query, *values, limit]).fetchall()
            except sqlite3.OperationalError as exc:
                raise MemoryStorageError("Memory full-text search failed.") from exc
        return [MemorySearchResult(self._item_from_row(row), float(row["search_rank"])) for row in rows]

    def add_evidence(self, evidence: MemoryEvidence) -> MemoryEvidence:
        digest = evidence.content_sha256 or hashlib.sha256(evidence.excerpt.encode("utf-8")).hexdigest()
        stored = replace(evidence, content_sha256=digest)
        with self._connection(create=True) as connection:
            assert connection is not None
            try:
                connection.execute(
                    """INSERT INTO memory_evidence
                    (evidence_id,memory_id,session_id,turn_id,excerpt,source_kind,content_sha256,created_at)
                    VALUES (?,?,?,?,?,?,?,?)""",
                    (
                        stored.evidence_id,
                        stored.memory_id,
                        stored.session_id,
                        stored.turn_id,
                        stored.excerpt,
                        stored.source_kind,
                        stored.content_sha256,
                        stored.created_at,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise MemoryConflictError("Memory evidence already exists or references an unknown item.") from exc
        return stored

    def list_evidence(self, *, memory_id: str | None = None, session_id: str | None = None) -> list[MemoryEvidence]:
        clauses: list[str] = []
        values: list[object] = []
        if memory_id is not None:
            clauses.append("memory_id=?")
            values.append(memory_id)
        if session_id is not None:
            clauses.append("session_id=?")
            values.append(session_id)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connection(create=False) as connection:
            if connection is None:
                return []
            rows = connection.execute(
                f"SELECT * FROM memory_evidence{where} ORDER BY created_at,evidence_id", values
            ).fetchall()
        return [self._evidence_from_row(row) for row in rows]

    def remove_evidence(self, evidence_id: str) -> None:
        with self._connection(create=True) as connection:
            assert connection is not None
            cursor = connection.execute("DELETE FROM memory_evidence WHERE evidence_id=?", (evidence_id,))
            if cursor.rowcount != 1:
                raise MemoryNotFoundError("Memory evidence does not exist.")

    def add_candidate(self, candidate: MemoryCandidate) -> MemoryCandidate:
        with self._connection(create=True) as connection:
            assert connection is not None
            try:
                self._insert_candidate(connection, candidate)
            except sqlite3.IntegrityError as exc:
                raise MemoryConflictError("Memory candidate already exists or violates storage constraints.") from exc
        return candidate

    def get_candidate(self, candidate_id: str) -> MemoryCandidate | None:
        with self._connection(create=False) as connection:
            if connection is None:
                return None
            row = connection.execute("SELECT * FROM memory_candidates WHERE candidate_id=?", (candidate_id,)).fetchone()
        return self._candidate_from_row(row) if row is not None else None

    def list_candidates(
        self,
        *,
        status: MemoryCandidateStatus | None = None,
        session_id: str | None = None,
        limit: int = 100,
    ) -> list[MemoryCandidate]:
        _validate_limit(limit)
        clauses: list[str] = []
        values: list[object] = []
        if status is not None:
            clauses.append("status=?")
            values.append(status.value)
        if session_id is not None:
            clauses.append("session_id=?")
            values.append(session_id)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        values.append(limit)
        with self._connection(create=False) as connection:
            if connection is None:
                return []
            rows = connection.execute(
                f"SELECT * FROM memory_candidates{where} ORDER BY created_at,candidate_id LIMIT ?", values
            ).fetchall()
        return [self._candidate_from_row(row) for row in rows]

    def set_candidate_status(
        self, candidate_id: str, status: MemoryCandidateStatus, *, updated_at: str | None = None
    ) -> MemoryCandidate:
        if not isinstance(status, MemoryCandidateStatus):
            raise ValueError("status must be a MemoryCandidateStatus value.")
        now = _canonical_utc_timestamp(updated_at, "updated_at") if updated_at is not None else utc_now()
        with self._connection(create=True, immediate=True) as connection:
            assert connection is not None
            existing = connection.execute(
                "SELECT status FROM memory_candidates WHERE candidate_id=?", (candidate_id,)
            ).fetchone()
            if existing is None:
                raise MemoryNotFoundError("Memory candidate does not exist.")
            current = MemoryCandidateStatus(str(existing["status"]))
            if current is not MemoryCandidateStatus.PENDING and current is not status:
                raise MemoryConflictError("A decided memory candidate cannot change status.")
            cursor = connection.execute(
                "UPDATE memory_candidates SET status=?,updated_at=? WHERE candidate_id=?",
                (status.value, now, candidate_id),
            )
            assert cursor.rowcount == 1
            row = connection.execute("SELECT * FROM memory_candidates WHERE candidate_id=?", (candidate_id,)).fetchone()
        assert row is not None
        return self._candidate_from_row(row)

    def delete_candidate(self, candidate_id: str) -> None:
        with self._connection(create=True) as connection:
            assert connection is not None
            cursor = connection.execute("DELETE FROM memory_candidates WHERE candidate_id=?", (candidate_id,))
            if cursor.rowcount != 1:
                raise MemoryNotFoundError("Memory candidate does not exist.")

    def record_extraction_batch(
        self, candidates: Sequence[MemoryCandidate], watermark: MemoryWatermark
    ) -> tuple[MemoryCandidate, ...]:
        """Persist Phase-1 candidates and their watermark in one transaction."""

        if any(candidate.session_id != watermark.source_id for candidate in candidates):
            raise ValueError("Extraction candidates must match the watermark source.")
        if any(candidate.status is not MemoryCandidateStatus.PENDING for candidate in candidates):
            raise ValueError("Extraction batches may contain only pending candidates.")
        with self._connection(create=True) as connection:
            assert connection is not None
            current = self._watermark_row(connection, watermark.source_id)
            if current is not None and int(current["position"]) > watermark.position:
                raise MemoryConflictError("Memory watermark cannot move backwards.")
            try:
                for candidate in candidates:
                    self._insert_candidate(connection, candidate)
                self._upsert_watermark(connection, watermark)
            except sqlite3.IntegrityError as exc:
                raise MemoryConflictError("Extraction batch conflicts with existing memory state.") from exc
        return tuple(candidates)

    def record_phase1_batch(
        self, records: Sequence[EpisodicMemoryRecord], watermark: MemoryWatermark
    ) -> tuple[EpisodicMemoryRecord, ...]:
        """Atomically persist episodic items, evidence, candidates, and watermark."""

        if any(record.candidate.session_id != watermark.source_id for record in records):
            raise ValueError("Phase-1 records must match the watermark source.")
        with self._connection(create=True, immediate=True) as connection:
            assert connection is not None
            current = self._watermark_row(connection, watermark.source_id)
            if current is not None and int(current["position"]) > watermark.position:
                raise MemoryConflictError("Memory watermark cannot move backwards.")
            try:
                for record in records:
                    self._insert_or_match_item(connection, record.item)
                    self._insert_or_match_candidate(connection, record.candidate)
                    for evidence in record.evidence:
                        self._insert_or_match_evidence(connection, evidence)
                self._upsert_watermark(connection, watermark)
            except sqlite3.IntegrityError as exc:
                raise MemoryConflictError("Phase-1 batch conflicts with existing memory state.") from exc
        return tuple(records)

    def list_phase1_records(self, *, limit: int = 100) -> list[EpisodicMemoryRecord]:
        """Load pending episodic candidates together with their authoritative evidence."""

        candidates = self.list_candidates(status=MemoryCandidateStatus.PENDING, limit=limit)
        records: list[EpisodicMemoryRecord] = []
        for candidate in candidates:
            if candidate.kind is not MemoryKind.EPISODIC or candidate.memory_id is None:
                continue
            item = self.get_item(candidate.memory_id)
            if item is None:
                raise MemorySchemaError("Pending episodic candidate references a missing memory item.")
            evidence = tuple(self.list_evidence(memory_id=item.memory_id))
            try:
                records.append(EpisodicMemoryRecord(candidate, item, evidence))
            except ValueError as exc:
                raise MemorySchemaError("Pending episodic candidate has invalid evidence.") from exc
        return records

    def get_watermark(self, source_id: str) -> MemoryWatermark | None:
        with self._connection(create=False) as connection:
            if connection is None:
                return None
            row = self._watermark_row(connection, source_id)
        return self._watermark_from_row(row) if row is not None else None

    def advance_watermark(self, watermark: MemoryWatermark) -> MemoryWatermark:
        with self._connection(create=True) as connection:
            assert connection is not None
            current = self._watermark_row(connection, watermark.source_id)
            if current is not None and int(current["position"]) > watermark.position:
                raise MemoryConflictError("Memory watermark cannot move backwards.")
            self._upsert_watermark(connection, watermark)
        return watermark

    def enqueue_job(self, job: MemoryJob) -> MemoryJob:
        with self._connection(create=True) as connection:
            assert connection is not None
            try:
                connection.execute(
                    """INSERT INTO memory_jobs
                    (job_id,kind,status,source_id,project_id,attempts,max_attempts,available_at,lease_owner,
                    lease_expires_at,last_error,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    self._job_values(job),
                )
            except sqlite3.IntegrityError as exc:
                raise MemoryConflictError("Memory job already exists or violates storage constraints.") from exc
        return job

    def enqueue_job_if_absent(self, job: MemoryJob) -> tuple[MemoryJob, bool]:
        """Atomically enqueue work unless the same source already has active work."""

        with self._connection(create=True, immediate=True) as connection:
            assert connection is not None
            row = connection.execute(
                """SELECT * FROM memory_jobs WHERE kind=? AND IFNULL(source_id,'')=IFNULL(?, '')
                AND IFNULL(project_id,'')=IFNULL(?, '') AND status IN ('pending','running')
                ORDER BY created_at,job_id LIMIT 1""",
                (job.kind.value, job.source_id, job.project_id),
            ).fetchone()
            if row is not None:
                return self._job_from_row(row), False
            try:
                connection.execute(
                    """INSERT INTO memory_jobs
                    (job_id,kind,status,source_id,project_id,attempts,max_attempts,available_at,lease_owner,
                    lease_expires_at,last_error,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    self._job_values(job),
                )
            except sqlite3.IntegrityError as exc:
                raise MemoryConflictError("Memory job already exists or violates storage constraints.") from exc
        return job, True

    def get_job(self, job_id: str) -> MemoryJob | None:
        with self._connection(create=False) as connection:
            if connection is None:
                return None
            row = connection.execute("SELECT * FROM memory_jobs WHERE job_id=?", (job_id,)).fetchone()
        return self._job_from_row(row) if row is not None else None

    def list_jobs(self, *, status: MemoryJobStatus | None = None, limit: int = 100) -> list[MemoryJob]:
        _validate_limit(limit)
        query = "SELECT * FROM memory_jobs"
        values: list[object] = []
        if status is not None:
            query += " WHERE status=?"
            values.append(status.value)
        query += " ORDER BY created_at,job_id LIMIT ?"
        values.append(limit)
        with self._connection(create=False) as connection:
            if connection is None:
                return []
            rows = connection.execute(query, values).fetchall()
        return [self._job_from_row(row) for row in rows]

    def claim_job(
        self,
        worker_id: str,
        *,
        kind: MemoryJobKind | None = None,
        now: str | None = None,
        lease_seconds: int = 3600,
    ) -> MemoryJob | None:
        if not worker_id or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,199}", worker_id):
            raise ValueError("worker_id must be a safe identifier.")
        if not isinstance(lease_seconds, int) or isinstance(lease_seconds, bool) or lease_seconds < 1:
            raise ValueError("lease_seconds must be a positive integer.")
        current = _parse_or_now(now)
        current_text = current.isoformat()
        expires = (current + timedelta(seconds=lease_seconds)).isoformat()
        with self._connection(create=True, immediate=True) as connection:
            assert connection is not None
            connection.execute(
                """UPDATE memory_jobs SET status='failed',lease_owner=NULL,lease_expires_at=NULL,
                last_error='Lease expired after maximum attempts.',updated_at=?
                WHERE status='running' AND lease_expires_at<=? AND attempts>=max_attempts""",
                (current_text, current_text),
            )
            clauses = [
                "attempts < max_attempts",
                "available_at <= ?",
                "(status='pending' OR (status='running' AND lease_expires_at <= ?))",
            ]
            values: list[object] = [current_text, current_text]
            if kind is not None:
                clauses.append("kind=?")
                values.append(kind.value)
            row = connection.execute(
                f"SELECT * FROM memory_jobs WHERE {' AND '.join(clauses)} ORDER BY created_at,job_id LIMIT 1",
                values,
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                """UPDATE memory_jobs SET status='running',attempts=attempts+1,lease_owner=?,
                lease_expires_at=?,updated_at=? WHERE job_id=?""",
                (worker_id, expires, current_text, row["job_id"]),
            )
            claimed = connection.execute("SELECT * FROM memory_jobs WHERE job_id=?", (row["job_id"],)).fetchone()
        assert claimed is not None
        return self._job_from_row(claimed)

    def complete_job(self, job_id: str, worker_id: str, *, completed_at: str | None = None) -> MemoryJob:
        now = _canonical_utc_timestamp(completed_at, "completed_at") if completed_at is not None else utc_now()
        with self._connection(create=True) as connection:
            assert connection is not None
            cursor = connection.execute(
                """UPDATE memory_jobs SET status='succeeded',lease_owner=NULL,lease_expires_at=NULL,
                last_error='',updated_at=? WHERE job_id=? AND status='running' AND lease_owner=?""",
                (now, job_id, worker_id),
            )
            if cursor.rowcount != 1:
                raise MemoryConflictError("Memory job is not leased by this worker.")
            row = connection.execute("SELECT * FROM memory_jobs WHERE job_id=?", (job_id,)).fetchone()
        assert row is not None
        return self._job_from_row(row)

    def fail_job(
        self,
        job_id: str,
        worker_id: str,
        error: str,
        *,
        retry_at: str | None = None,
        failed_at: str | None = None,
    ) -> MemoryJob:
        now = _canonical_utc_timestamp(failed_at, "failed_at") if failed_at is not None else utc_now()
        available = _canonical_utc_timestamp(retry_at, "retry_at") if retry_at is not None else now
        with self._connection(create=True, immediate=True) as connection:
            assert connection is not None
            row = connection.execute(
                "SELECT * FROM memory_jobs WHERE job_id=? AND status='running' AND lease_owner=?",
                (job_id, worker_id),
            ).fetchone()
            if row is None:
                raise MemoryConflictError("Memory job is not leased by this worker.")
            terminal = int(row["attempts"]) >= int(row["max_attempts"])
            status = MemoryJobStatus.FAILED.value if terminal else MemoryJobStatus.PENDING.value
            connection.execute(
                """UPDATE memory_jobs SET status=?,available_at=?,lease_owner=NULL,lease_expires_at=NULL,
                last_error=?,updated_at=? WHERE job_id=?""",
                (status, available, str(error)[: 4 * 1024], now, job_id),
            )
            updated = connection.execute("SELECT * FROM memory_jobs WHERE job_id=?", (job_id,)).fetchone()
        assert updated is not None
        return self._job_from_row(updated)

    def cancel_job(self, job_id: str, *, cancelled_at: str | None = None, reason: str = "") -> MemoryJob:
        """Persist cooperative cancellation for pending or running work."""

        now = _canonical_utc_timestamp(cancelled_at, "cancelled_at") if cancelled_at is not None else utc_now()
        safe_reason = str(reason)[: 4 * 1024]
        with self._connection(create=True, immediate=True) as connection:
            assert connection is not None
            cursor = connection.execute(
                """UPDATE memory_jobs SET status='cancelled',lease_owner=NULL,lease_expires_at=NULL,
                last_error=?,updated_at=? WHERE job_id=? AND status IN ('pending','running')""",
                (safe_reason, now, job_id),
            )
            if cursor.rowcount != 1:
                raise MemoryNotFoundError("Active memory job does not exist.")
            row = connection.execute("SELECT * FROM memory_jobs WHERE job_id=?", (job_id,)).fetchone()
        assert row is not None
        return self._job_from_row(row)

    def clear_all(self) -> None:
        """Clear user memory data atomically while preserving the database schema."""

        with self._connection(create=True, immediate=True) as connection:
            assert connection is not None
            connection.execute("DELETE FROM memory_evidence")
            connection.execute("DELETE FROM memory_candidates")
            connection.execute("DELETE FROM memory_items")
            connection.execute("DELETE FROM memory_watermarks")
            connection.execute("DELETE FROM memory_jobs")

    def apply_selection_diff(self, selection: MemorySelectionDiff) -> None:
        now = utc_now()
        with self._connection(create=True, immediate=True) as connection:
            assert connection is not None
            try:
                for item in selection.added:
                    self._insert_item(connection, item)
                for memory_id in selection.retained_ids:
                    cursor = connection.execute(
                        """UPDATE memory_items SET status='active',deleted_at=NULL,updated_at=?
                        WHERE memory_id=? AND status!='deleted'""",
                        (now, memory_id),
                    )
                    if cursor.rowcount != 1:
                        raise MemoryNotFoundError("Retained memory item does not exist.")
                for memory_id in selection.removed_ids:
                    cursor = connection.execute(
                        """UPDATE memory_items SET status='deleted',deleted_at=?,updated_at=?
                        WHERE memory_id=? AND status!='deleted'""",
                        (now, now, memory_id),
                    )
                    if cursor.rowcount != 1:
                        raise MemoryNotFoundError("Removed memory item does not exist.")
            except sqlite3.IntegrityError as exc:
                raise MemoryConflictError("Memory selection diff conflicts with existing state.") from exc

    def apply_consolidation_batch(
        self,
        selection: MemorySelectionDiff,
        evidence: Sequence[MemoryEvidence],
        *,
        selected_candidate_ids: Sequence[str],
        rejected_candidate_ids: Sequence[str],
    ) -> None:
        """Apply one Phase-2 decision, evidence links, and candidate states atomically."""

        selected = tuple(selected_candidate_ids)
        rejected = tuple(rejected_candidate_ids)
        if len(set(selected)) != len(selected) or len(set(rejected)) != len(rejected):
            raise ValueError("Consolidation candidate ids must be unique.")
        if set(selected) & set(rejected):
            raise ValueError("Selected and rejected candidate ids must be disjoint.")
        added_ids = {item.memory_id for item in selection.added}
        if any(item.kind is MemoryKind.EPISODIC for item in selection.added):
            raise ValueError("Phase-2 may add only semantic or procedural memories.")
        if any(item.status is not MemoryStatus.ACTIVE for item in selection.added):
            raise ValueError("Phase-2 added memories must be active.")
        evidence_targets = {source.memory_id for source in evidence}
        if not added_ids <= evidence_targets:
            raise ValueError("Every added long-term memory requires evidence.")

        now = utc_now()
        with self._connection(create=True, immediate=True) as connection:
            assert connection is not None
            all_candidate_ids = (*selected, *rejected)
            if all_candidate_ids:
                placeholders = ",".join("?" for _ in all_candidate_ids)
                rows = connection.execute(
                    f"SELECT candidate_id,status FROM memory_candidates WHERE candidate_id IN ({placeholders})",
                    all_candidate_ids,
                ).fetchall()
                statuses = {str(row["candidate_id"]): str(row["status"]) for row in rows}
                if set(statuses) != set(all_candidate_ids):
                    raise MemoryNotFoundError("Consolidation references an unknown candidate.")
                if any(status != MemoryCandidateStatus.PENDING.value for status in statuses.values()):
                    raise MemoryConflictError("Consolidation candidates must still be pending.")
            try:
                for item in selection.added:
                    self._insert_item(connection, item)
                for memory_id in selection.retained_ids:
                    cursor = connection.execute(
                        """UPDATE memory_items SET status='active',deleted_at=NULL,updated_at=?
                        WHERE memory_id=? AND status!='deleted' AND kind!='episodic'""",
                        (now, memory_id),
                    )
                    if cursor.rowcount != 1:
                        raise MemoryNotFoundError("Retained long-term memory does not exist.")
                for memory_id in selection.removed_ids:
                    cursor = connection.execute(
                        """UPDATE memory_items SET status='deleted',deleted_at=?,updated_at=?
                        WHERE memory_id=? AND status!='deleted' AND kind!='episodic'""",
                        (now, now, memory_id),
                    )
                    if cursor.rowcount != 1:
                        raise MemoryNotFoundError("Removed long-term memory does not exist.")
                for source in evidence:
                    target = connection.execute(
                        "SELECT kind FROM memory_items WHERE memory_id=?",
                        (source.memory_id,),
                    ).fetchone()
                    if target is None or str(target["kind"]) == MemoryKind.EPISODIC.value:
                        raise MemoryConflictError("Consolidation evidence must target a long-term memory.")
                    self._insert_or_match_evidence(connection, source)
                for candidate_id in selected:
                    connection.execute(
                        "UPDATE memory_candidates SET status='selected',updated_at=? WHERE candidate_id=?",
                        (now, candidate_id),
                    )
                for candidate_id in rejected:
                    connection.execute(
                        "UPDATE memory_candidates SET status='rejected',updated_at=? WHERE candidate_id=?",
                        (now, candidate_id),
                    )
            except sqlite3.IntegrityError as exc:
                raise MemoryConflictError("Memory consolidation conflicts with existing state.") from exc

    def rebuild_projections(self) -> tuple[Path, tuple[Path, ...]]:
        """Rebuild all Markdown projections from the authoritative database."""

        self.ensure()
        with self._connection(create=False) as connection:
            assert connection is not None
            rows = connection.execute(
                "SELECT * FROM memory_items WHERE status!='deleted' ORDER BY kind,updated_at,memory_id"
            ).fetchall()
        items = [self._item_from_row(row) for row in rows]
        evidence = self.list_evidence()
        item_by_id = {item.memory_id: item for item in items}
        raw_content = self._render_raw_memories(items)
        _atomic_write_text(self.paths.raw_memories_file, raw_content)

        grouped: dict[str, list[MemoryEvidence]] = {}
        for source in evidence:
            if source.memory_id in item_by_id:
                grouped.setdefault(source.session_id, []).append(source)
        expected: set[Path] = set()
        for session_id, records in grouped.items():
            target = self.paths.rollout_summaries_dir / f"{session_id}.md"
            expected.add(target)
            _atomic_write_text(target, self._render_rollout_summary(session_id, records, item_by_id))
        for existing in self.paths.rollout_summaries_dir.glob("*.md"):
            if existing.is_symlink():
                raise MemoryStorageError("Memory rollout projection cannot be a symbolic link.")
            if not existing.is_file():
                raise MemoryStorageError("Memory rollout projection path must be a regular file.")
            if existing not in expected:
                try:
                    existing.unlink()
                except OSError as exc:
                    raise MemoryStorageError("Unable to remove stale memory projection.") from exc
        return self.paths.raw_memories_file, tuple(sorted(expected))

    @contextmanager
    def _connection(self, *, create: bool, immediate: bool = False) -> Iterator[sqlite3.Connection | None]:
        with self._lock:
            if create:
                self._ensure_schema()
            elif not self.paths.memory_db.exists():
                yield None
                return
            self._validate_existing_paths()
            connection = sqlite3.connect(self.paths.memory_db, timeout=10)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 10000")
            try:
                version = int(connection.execute("PRAGMA user_version").fetchone()[0])
                if version != MEMORY_SCHEMA_VERSION:
                    raise MemorySchemaError(
                        f"Unsupported memory schema version {version}; expected {MEMORY_SCHEMA_VERSION}."
                    )
                if immediate:
                    connection.execute("BEGIN IMMEDIATE")
                yield connection
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

    def _ensure_schema(self) -> None:
        try:
            self.paths.ensure_memories()
        except ConfigurationError as exc:
            raise MemoryStorageError(str(exc)) from exc
        self._validate_existing_paths()
        if self._schema_ready and self.paths.memory_db.is_file():
            return
        connection = sqlite3.connect(self.paths.memory_db, timeout=10)
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version > MEMORY_SCHEMA_VERSION:
                raise MemorySchemaError(
                    f"Memory schema version {version} is newer than supported version {MEMORY_SCHEMA_VERSION}."
                )
            if version == 0:
                tables = {
                    str(row[0])
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type IN ('table','view') AND name NOT LIKE 'sqlite_%'"
                    )
                }
                if tables:
                    raise MemorySchemaError("Unversioned non-empty memory database cannot be migrated safely.")
                try:
                    connection.executescript(MEMORY_SCHEMA)
                except sqlite3.OperationalError as exc:
                    raise MemorySchemaError("SQLite FTS5 support is required for memory storage.") from exc
                now = utc_now()
                connection.execute(
                    "INSERT INTO memory_metadata(singleton,schema_version,created_at,updated_at) VALUES (1,?,?,?)",
                    (MEMORY_SCHEMA_VERSION, now, now),
                )
                connection.commit()
            elif version == 1:
                self._migrate_v1_to_v2(connection)
                self._migrate_v2_to_v3(connection)
            elif version == 2:
                self._migrate_v2_to_v3(connection)
            self._validate_schema(connection)
            connection.execute("PRAGMA journal_mode = WAL")
            self._schema_ready = True
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _validate_existing_paths(self) -> None:
        if self.paths.memories_dir.is_symlink():
            raise MemoryStorageError("Memories directory cannot be a symbolic link.")
        if self.paths.memory_db.is_symlink():
            raise MemoryStorageError("Memory database cannot be a symbolic link.")
        if self.paths.memory_db.exists() and not self.paths.memory_db.is_file():
            raise MemoryStorageError("Memory database path must be a regular file.")

    @staticmethod
    def _validate_schema(connection: sqlite3.Connection) -> None:
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version != MEMORY_SCHEMA_VERSION:
            raise MemorySchemaError(f"Unsupported memory schema version {version}; expected {MEMORY_SCHEMA_VERSION}.")
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','view') AND name NOT LIKE 'sqlite_%'"
            )
        }
        missing = _REQUIRED_TABLES - tables
        if missing:
            raise MemorySchemaError("Memory database is missing required tables.")
        triggers = {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
        if _REQUIRED_TRIGGERS - triggers:
            raise MemorySchemaError("Memory database is missing required FTS synchronization triggers.")
        indexes = {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        if _REQUIRED_INDEXES - indexes:
            raise MemorySchemaError("Memory database is missing required uniqueness indexes.")
        row = connection.execute("SELECT schema_version FROM memory_metadata WHERE singleton=1").fetchone()
        if row is None or int(row[0]) != MEMORY_SCHEMA_VERSION:
            raise MemorySchemaError("Memory metadata version is invalid.")

    @staticmethod
    def _migrate_v1_to_v2(connection: sqlite3.Connection) -> None:
        """Add the candidate-to-episodic link required by the manual pipeline."""

        try:
            connection.executescript(
                """
                BEGIN IMMEDIATE;
                ALTER TABLE memory_candidates
                    ADD COLUMN memory_id TEXT REFERENCES memory_items(memory_id) ON DELETE SET NULL;
                CREATE UNIQUE INDEX memory_candidates_memory_idx
                    ON memory_candidates(memory_id) WHERE memory_id IS NOT NULL;
                ALTER TABLE memory_metadata RENAME TO memory_metadata_v1;
                CREATE TABLE memory_metadata (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    schema_version INTEGER NOT NULL CHECK (schema_version = 2),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                INSERT INTO memory_metadata(singleton,schema_version,created_at,updated_at)
                    SELECT singleton,2,created_at,updated_at FROM memory_metadata_v1;
                DROP TABLE memory_metadata_v1;
                PRAGMA user_version = 2;
                COMMIT;
                """
            )
        except sqlite3.Error as exc:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise MemorySchemaError("Unable to migrate memory schema from version 1 to 2.") from exc

    @staticmethod
    def _migrate_v2_to_v3(connection: sqlite3.Connection) -> None:
        """Add the disabled lifecycle state and active-job idempotency."""

        try:
            connection.executescript(
                """
                PRAGMA foreign_keys = OFF;
                PRAGMA legacy_alter_table = ON;
                BEGIN IMMEDIATE;
                DROP TRIGGER memory_items_fts_insert;
                DROP TRIGGER memory_items_fts_delete;
                DROP TRIGGER memory_items_fts_update;
                ALTER TABLE memory_items RENAME TO memory_items_v2;
                CREATE TABLE memory_items (
                    memory_id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL CHECK (kind IN ('episodic','semantic','procedural')),
                    title TEXT NOT NULL,
                    content TEXT NOT NULL,
                    summary TEXT NOT NULL DEFAULT '',
                    scope TEXT NOT NULL CHECK (scope IN ('global','project')),
                    project_id TEXT,
                    confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
                    tags_json TEXT NOT NULL DEFAULT '[]',
                    status TEXT NOT NULL CHECK (status IN ('active','disabled','superseded','deleted')),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_used_at TEXT,
                    deleted_at TEXT,
                    CHECK ((scope = 'global' AND project_id IS NULL) OR (scope = 'project' AND project_id IS NOT NULL)),
                    CHECK ((status = 'deleted' AND deleted_at IS NOT NULL) OR (status != 'deleted' AND deleted_at IS NULL))
                );
                INSERT INTO memory_items SELECT * FROM memory_items_v2;
                DROP TABLE memory_items_v2;
                CREATE INDEX memory_items_scope_idx ON memory_items(scope,project_id,status,updated_at);
                CREATE INDEX memory_items_kind_idx ON memory_items(kind,status,updated_at);
                CREATE TRIGGER memory_items_fts_insert AFTER INSERT ON memory_items
                WHEN new.status = 'active'
                BEGIN
                    INSERT INTO memory_items_fts(memory_id,title,summary,content,tags)
                    VALUES (new.memory_id,new.title,new.summary,new.content,new.tags_json);
                END;
                CREATE TRIGGER memory_items_fts_delete AFTER DELETE ON memory_items
                BEGIN
                    DELETE FROM memory_items_fts WHERE memory_id = old.memory_id;
                END;
                CREATE TRIGGER memory_items_fts_update AFTER UPDATE ON memory_items
                BEGIN
                    DELETE FROM memory_items_fts WHERE memory_id = old.memory_id;
                    INSERT INTO memory_items_fts(memory_id,title,summary,content,tags)
                    SELECT new.memory_id,new.title,new.summary,new.content,new.tags_json
                    WHERE new.status = 'active';
                END;
                DELETE FROM memory_items_fts;
                INSERT INTO memory_items_fts(memory_id,title,summary,content,tags)
                    SELECT memory_id,title,summary,content,tags_json
                    FROM memory_items WHERE status='active';
                UPDATE memory_jobs
                SET status='cancelled',lease_owner=NULL,lease_expires_at=NULL,
                    last_error='duplicate_cancelled_during_migration'
                WHERE job_id IN (
                    SELECT job_id FROM (
                        SELECT job_id,ROW_NUMBER() OVER (
                            PARTITION BY kind,IFNULL(source_id,''),IFNULL(project_id,'')
                            ORDER BY created_at,job_id
                        ) AS duplicate_number
                        FROM memory_jobs WHERE status IN ('pending','running')
                    ) WHERE duplicate_number > 1
                );
                CREATE UNIQUE INDEX IF NOT EXISTS memory_jobs_active_source_idx
                    ON memory_jobs(kind,IFNULL(source_id,''),IFNULL(project_id,''))
                    WHERE status IN ('pending','running');
                ALTER TABLE memory_metadata RENAME TO memory_metadata_v2;
                CREATE TABLE memory_metadata (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    schema_version INTEGER NOT NULL CHECK (schema_version = 3),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                INSERT INTO memory_metadata(singleton,schema_version,created_at,updated_at)
                    SELECT singleton,3,created_at,updated_at FROM memory_metadata_v2;
                DROP TABLE memory_metadata_v2;
                PRAGMA user_version = 3;
                COMMIT;
                PRAGMA legacy_alter_table = OFF;
                PRAGMA foreign_keys = ON;
                """
            )
        except sqlite3.Error as exc:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise MemorySchemaError("Unable to migrate memory schema from version 2 to 3.") from exc

    @staticmethod
    def _insert_item(connection: sqlite3.Connection, item: MemoryItem) -> None:
        connection.execute(
            """INSERT INTO memory_items
            (memory_id,kind,title,content,summary,scope,project_id,confidence,tags_json,status,created_at,
            updated_at,last_used_at,deleted_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                item.memory_id,
                item.kind.value,
                item.title,
                item.content,
                item.summary,
                item.scope.value,
                item.project_id,
                float(item.confidence),
                json.dumps(item.tags, ensure_ascii=False, separators=(",", ":")),
                item.status.value,
                item.created_at,
                item.updated_at,
                item.last_used_at,
                item.deleted_at,
            ),
        )

    @classmethod
    def _insert_or_match_item(cls, connection: sqlite3.Connection, item: MemoryItem) -> None:
        row = connection.execute("SELECT * FROM memory_items WHERE memory_id=?", (item.memory_id,)).fetchone()
        if row is None:
            cls._insert_item(connection, item)
            return
        if cls._item_from_row(row) != item:
            raise MemoryConflictError("Episodic memory id already contains different data.")

    @staticmethod
    def _item_update_values(item: MemoryItem) -> tuple[object, ...]:
        return (
            item.kind.value,
            item.title,
            item.content,
            item.summary,
            item.scope.value,
            item.project_id,
            float(item.confidence),
            json.dumps(item.tags, ensure_ascii=False, separators=(",", ":")),
            item.status.value,
            item.updated_at,
            item.last_used_at,
            item.deleted_at,
            item.memory_id,
        )

    @staticmethod
    def _item_from_row(row: sqlite3.Row) -> MemoryItem:
        tags = json.loads(str(row["tags_json"]))
        return MemoryItem(
            memory_id=str(row["memory_id"]),
            kind=MemoryKind(str(row["kind"])),
            title=str(row["title"]),
            content=str(row["content"]),
            summary=str(row["summary"]),
            scope=MemoryScope(str(row["scope"])),
            project_id=str(row["project_id"]) if row["project_id"] is not None else None,
            confidence=float(row["confidence"]),
            tags=tuple(str(tag) for tag in tags),
            status=MemoryStatus(str(row["status"])),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            last_used_at=str(row["last_used_at"]) if row["last_used_at"] is not None else None,
            deleted_at=str(row["deleted_at"]) if row["deleted_at"] is not None else None,
        )

    @staticmethod
    def _insert_candidate(connection: sqlite3.Connection, candidate: MemoryCandidate) -> None:
        connection.execute(
            """INSERT INTO memory_candidates
            (candidate_id,kind,content,summary,session_id,turn_id,project_id,memory_id,confidence,status,
            created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                candidate.candidate_id,
                candidate.kind.value,
                candidate.content,
                candidate.summary,
                candidate.session_id,
                candidate.turn_id,
                candidate.project_id,
                candidate.memory_id,
                float(candidate.confidence),
                candidate.status.value,
                candidate.created_at,
                candidate.updated_at,
            ),
        )

    @classmethod
    def _insert_or_match_candidate(cls, connection: sqlite3.Connection, candidate: MemoryCandidate) -> None:
        row = connection.execute(
            "SELECT * FROM memory_candidates WHERE candidate_id=?", (candidate.candidate_id,)
        ).fetchone()
        if row is None:
            cls._insert_candidate(connection, candidate)
            return
        if cls._candidate_from_row(row) != candidate:
            raise MemoryConflictError("Memory candidate id already contains different data.")

    @classmethod
    def _insert_or_match_evidence(cls, connection: sqlite3.Connection, evidence: MemoryEvidence) -> None:
        digest = evidence.content_sha256 or hashlib.sha256(evidence.excerpt.encode("utf-8")).hexdigest()
        stored = replace(evidence, content_sha256=digest)
        row = connection.execute("SELECT * FROM memory_evidence WHERE evidence_id=?", (stored.evidence_id,)).fetchone()
        if row is None:
            connection.execute(
                """INSERT INTO memory_evidence
                (evidence_id,memory_id,session_id,turn_id,excerpt,source_kind,content_sha256,created_at)
                VALUES (?,?,?,?,?,?,?,?)""",
                (
                    stored.evidence_id,
                    stored.memory_id,
                    stored.session_id,
                    stored.turn_id,
                    stored.excerpt,
                    stored.source_kind,
                    stored.content_sha256,
                    stored.created_at,
                ),
            )
            return
        if cls._evidence_from_row(row) != stored:
            raise MemoryConflictError("Memory evidence id already contains different data.")

    @staticmethod
    def _candidate_from_row(row: sqlite3.Row) -> MemoryCandidate:
        return MemoryCandidate(
            candidate_id=str(row["candidate_id"]),
            kind=MemoryKind(str(row["kind"])),
            content=str(row["content"]),
            summary=str(row["summary"]),
            session_id=str(row["session_id"]),
            turn_id=str(row["turn_id"]) if row["turn_id"] is not None else None,
            project_id=str(row["project_id"]) if row["project_id"] is not None else None,
            memory_id=str(row["memory_id"]) if row["memory_id"] is not None else None,
            confidence=float(row["confidence"]),
            status=MemoryCandidateStatus(str(row["status"])),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    @staticmethod
    def _evidence_from_row(row: sqlite3.Row) -> MemoryEvidence:
        return MemoryEvidence(
            evidence_id=str(row["evidence_id"]),
            memory_id=str(row["memory_id"]),
            session_id=str(row["session_id"]),
            turn_id=str(row["turn_id"]) if row["turn_id"] is not None else None,
            excerpt=str(row["excerpt"]),
            source_kind=str(row["source_kind"]),
            content_sha256=str(row["content_sha256"]),
            created_at=str(row["created_at"]),
        )

    @staticmethod
    def _watermark_row(connection: sqlite3.Connection, source_id: str) -> sqlite3.Row | None:
        return connection.execute("SELECT * FROM memory_watermarks WHERE source_id=?", (source_id,)).fetchone()

    @staticmethod
    def _upsert_watermark(connection: sqlite3.Connection, watermark: MemoryWatermark) -> None:
        connection.execute(
            """INSERT INTO memory_watermarks(source_id,position,event_id,updated_at) VALUES (?,?,?,?)
            ON CONFLICT(source_id) DO UPDATE SET position=excluded.position,event_id=excluded.event_id,
            updated_at=excluded.updated_at""",
            (watermark.source_id, watermark.position, watermark.event_id, watermark.updated_at),
        )

    @staticmethod
    def _watermark_from_row(row: sqlite3.Row) -> MemoryWatermark:
        return MemoryWatermark(
            source_id=str(row["source_id"]),
            position=int(row["position"]),
            event_id=str(row["event_id"]) if row["event_id"] is not None else None,
            updated_at=str(row["updated_at"]),
        )

    @staticmethod
    def _job_values(job: MemoryJob) -> tuple[object, ...]:
        return (
            job.job_id,
            job.kind.value,
            job.status.value,
            job.source_id,
            job.project_id,
            job.attempts,
            job.max_attempts,
            job.available_at,
            job.lease_owner,
            job.lease_expires_at,
            job.last_error,
            job.created_at,
            job.updated_at,
        )

    @staticmethod
    def _job_from_row(row: sqlite3.Row) -> MemoryJob:
        return MemoryJob(
            job_id=str(row["job_id"]),
            kind=MemoryJobKind(str(row["kind"])),
            status=MemoryJobStatus(str(row["status"])),
            source_id=str(row["source_id"]) if row["source_id"] is not None else None,
            project_id=str(row["project_id"]) if row["project_id"] is not None else None,
            attempts=int(row["attempts"]),
            max_attempts=int(row["max_attempts"]),
            available_at=str(row["available_at"]),
            lease_owner=str(row["lease_owner"]) if row["lease_owner"] is not None else None,
            lease_expires_at=str(row["lease_expires_at"]) if row["lease_expires_at"] is not None else None,
            last_error=str(row["last_error"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    @staticmethod
    def _scope_clauses(project_id: str | None, *, prefix: str = "") -> tuple[list[str], list[object]]:
        if project_id is None:
            return [f"{prefix}scope='global'"], []
        return [f"({prefix}scope='global' OR ({prefix}scope='project' AND {prefix}project_id=?))"], [project_id]

    @staticmethod
    def _render_raw_memories(items: Sequence[MemoryItem]) -> str:
        lines = [
            "# Generated Memory Projection",
            "",
            "> Generated from memory.db. Do not edit this file as an authoritative source.",
            "",
        ]
        for kind in MemoryKind:
            selected = [item for item in items if item.kind is kind and item.status is MemoryStatus.ACTIVE]
            if not selected:
                continue
            lines.extend((f"## {kind.value.title()}", ""))
            for item in selected:
                lines.extend(
                    (
                        f"### {_markdown_heading(item.title)}",
                        "",
                        f"- ID: `{item.memory_id}`",
                        f"- Scope: `{item.scope.value}`",
                        f"- Confidence: `{item.confidence:.3f}`",
                        "",
                        item.content.strip(),
                        "",
                    )
                )
        if len(lines) == 4:
            lines.extend(("_No active memories._", ""))
        return "\n".join(lines)

    @staticmethod
    def _render_rollout_summary(
        session_id: str, evidence: Sequence[MemoryEvidence], items: dict[str, MemoryItem]
    ) -> str:
        lines = [
            "# Generated Rollout Memory Evidence",
            "",
            "> Generated from memory.db. Do not edit this file as an authoritative source.",
            "",
            f"- Session: `{session_id}`",
            "",
        ]
        for source in evidence:
            item = items[source.memory_id]
            lines.extend(
                (
                    f"## {_markdown_heading(item.title)}",
                    "",
                    f"- Memory: `{item.memory_id}`",
                    f"- Evidence: `{source.evidence_id}`",
                    "",
                    source.excerpt.strip(),
                    "",
                )
            )
        return "\n".join(lines)


def _validate_limit(limit: int) -> None:
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100_000:
        raise ValueError("limit must be between 1 and 100000.")


def _fts_query(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("Memory search query must be a string.")
    tokens = _QUERY_TOKEN_RE.findall(value.strip())[:20]
    return " OR ".join(f'"{token}"' for token in tokens)


def _parse_or_now(value: str | None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("now must be an ISO-8601 timestamp.") from exc
    if parsed.tzinfo is None:
        raise ValueError("now must include a time zone.")
    return parsed.astimezone(UTC)


def _canonical_utc_timestamp(value: str, name: str) -> str:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an ISO-8601 timestamp.") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{name} must include a time zone.")
    return parsed.astimezone(UTC).isoformat()


def _markdown_heading(value: str) -> str:
    return " ".join(value.replace("#", "").replace("`", "'").split())[:500] or "Untitled"


def _atomic_write_text(path: Path, content: str) -> None:
    if path.is_symlink():
        raise MemoryStorageError("Memory projection cannot be a symbolic link.")
    if path.exists() and not path.is_file():
        raise MemoryStorageError("Memory projection path must be a regular file.")
    temporary = path.parent / f".{path.name}.{uuid4().hex}.tmp"
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except OSError as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise MemoryStorageError("Unable to rebuild memory projection.") from exc


__all__ = [
    "MEMORY_SCHEMA_VERSION",
    "MemoryConflictError",
    "MemoryNotFoundError",
    "MemorySchemaError",
    "MemoryStorageError",
    "MemoryStore",
]
