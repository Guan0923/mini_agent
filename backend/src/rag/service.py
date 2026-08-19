"""User-scoped knowledge-base persistence and hybrid retrieval."""

from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import tempfile
import time
from collections.abc import Iterable
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from uuid import uuid4

import requests

from .chunking import chunk_pdf_pages, pretokenize
from .models import (
    DocumentIngestion,
    EmbeddingProfile,
    KnowledgeBaseSection,
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeSearchResponse,
    KnowledgeSearchResult,
    PdfPage,
    RagAlgorithm,
)

RRF_K = 60


class RagDependencyError(RuntimeError):
    """An external embedding or vector dependency is unavailable."""


class RagNotFoundError(ValueError):
    """A user-scoped RAG section or document does not exist."""


class RagBusyError(RuntimeError):
    """A document cannot be mutated while indexing is active."""


class KnowledgeBaseService:
    """SQLite source of truth with optional Ollama and Qdrant derived indexes."""

    def __init__(
        self,
        root: Path,
        *,
        qdrant_url: str | None = None,
        session: requests.Session | None = None,
    ) -> None:
        self.root = Path(root).expanduser().resolve()
        self.rag_dir = self.root / "rag" if self.root.name != "rag" else self.root
        self.db_path = self.rag_dir / "knowledge.db"
        self.sections_dir = self.rag_dir / "sections"
        self.qdrant_url = (qdrant_url or os.environ.get("MINI_AGENT_QDRANT_URL", "http://127.0.0.1:6333")).rstrip("/")
        self.http = session or requests.Session()
        self.rag_dir.mkdir(parents=True, exist_ok=True)
        self.sections_dir.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @contextmanager
    def _connection(self):
        connection = sqlite3.connect(self.db_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def _init_db(self) -> None:
        with self._connection() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS sections (
                    section_id TEXT PRIMARY KEY, user_id TEXT NOT NULL,
                    section_type TEXT NOT NULL, project_id TEXT, session_id TEXT,
                    display_name TEXT NOT NULL, created_at REAL NOT NULL,
                    UNIQUE(user_id, project_id, session_id)
                );
                CREATE TABLE IF NOT EXISTS documents (
                    document_id TEXT PRIMARY KEY, user_id TEXT NOT NULL,
                    section_id TEXT NOT NULL REFERENCES sections(section_id) ON DELETE CASCADE,
                    filename TEXT NOT NULL, relative_path TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL, sha256 TEXT NOT NULL,
                    status TEXT NOT NULL, source TEXT NOT NULL, created_at REAL NOT NULL,
                    error TEXT, UNIQUE(user_id, section_id, sha256)
                );
                CREATE TABLE IF NOT EXISTS embedding_profiles (
                    profile_id TEXT PRIMARY KEY, provider TEXT NOT NULL, base_url TEXT NOT NULL,
                    model TEXT NOT NULL, dimension INTEGER NOT NULL, updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS ingestions (
                    document_id TEXT NOT NULL REFERENCES documents(document_id) ON DELETE CASCADE,
                    embedding_profile_id TEXT NOT NULL REFERENCES embedding_profiles(profile_id),
                    status TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL,
                    error TEXT, PRIMARY KEY(document_id, embedding_profile_id)
                );
                CREATE TABLE IF NOT EXISTS knowledge_chunks (
                    chunk_id TEXT PRIMARY KEY, user_id TEXT NOT NULL,
                    section_id TEXT NOT NULL, document_id TEXT NOT NULL REFERENCES documents(document_id) ON DELETE CASCADE,
                    text TEXT NOT NULL, tokenized_text TEXT NOT NULL, page_start INTEGER NOT NULL,
                    page_end INTEGER NOT NULL, sequence INTEGER NOT NULL, token_count INTEGER NOT NULL,
                    sha256 TEXT NOT NULL
                );
                CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
                    text, tokenized_text, content='knowledge_chunks', content_rowid='rowid', tokenize='unicode61'
                );
                """
            )

    @staticmethod
    def _row_section(row: sqlite3.Row) -> KnowledgeBaseSection:
        return KnowledgeBaseSection(**dict(row))

    @staticmethod
    def _row_document(row: sqlite3.Row) -> KnowledgeDocument:
        return KnowledgeDocument(**dict(row))

    def ensure_section(
        self,
        user_id: str,
        *,
        project_id: str | None = None,
        session_id: str | None = None,
        display_name: str | None = None,
    ) -> KnowledgeBaseSection:
        if bool(project_id) == bool(session_id):
            raise ValueError("exactly one of project_id or session_id is required")
        section_type = "project" if project_id else "session"
        label = display_name or (f"项目 {project_id}" if project_id else f"会话 {session_id}")
        with self._connection() as db:
            row = db.execute(
                "SELECT * FROM sections WHERE user_id=? AND project_id IS ? AND session_id IS ?",
                (user_id, project_id, session_id),
            ).fetchone()
            if row is None:
                now = time.time()
                section_id = uuid4().hex
                db.execute(
                    "INSERT INTO sections VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (section_id, user_id, section_type, project_id, session_id, label, now),
                )
                row = db.execute("SELECT * FROM sections WHERE section_id=?", (section_id,)).fetchone()
        return self._row_section(row)

    def register_profile(self, profile: EmbeddingProfile) -> EmbeddingProfile:
        with self._connection() as db:
            db.execute(
                "INSERT INTO embedding_profiles VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(profile_id) DO UPDATE SET updated_at=excluded.updated_at",
                (profile.profile_id, profile.provider, profile.base_url, profile.model, profile.dimension, time.time()),
            )
        return profile

    def list_sections(self, *, user_id: str) -> list[KnowledgeBaseSection]:
        """List the user's project/session partitions without creating any."""

        with self._connection() as db:
            rows = db.execute(
                """SELECT * FROM sections
                   WHERE user_id=?
                   ORDER BY section_type, display_name COLLATE NOCASE, created_at""",
                (user_id,),
            ).fetchall()
        return [self._row_section(row) for row in rows]

    def get_section(self, section_id: str, *, user_id: str) -> KnowledgeBaseSection:
        with self._connection() as db:
            row = db.execute(
                "SELECT * FROM sections WHERE section_id=? AND user_id=?",
                (section_id, user_id),
            ).fetchone()
        if row is None:
            raise RagNotFoundError("knowledge-base section not found")
        return self._row_section(row)

    def get_document(self, document_id: str, *, user_id: str) -> KnowledgeDocument:
        with self._connection() as db:
            row = db.execute(
                "SELECT * FROM documents WHERE document_id=? AND user_id=?",
                (document_id, user_id),
            ).fetchone()
        if row is None:
            raise RagNotFoundError("knowledge-base document not found")
        return self._row_document(row)

    def import_document(
        self,
        source_path: Path,
        *,
        user_id: str,
        section_id: str,
        profile: EmbeddingProfile,
        source: str = "upload",
    ) -> tuple[KnowledgeDocument, DocumentIngestion, bool]:
        source_path = Path(source_path).resolve()
        if source_path.suffix.lower() != ".pdf" or not source_path.is_file():
            raise ValueError("source_path must be an existing PDF")
        digest = hashlib.sha256(source_path.read_bytes()).hexdigest()
        self.register_profile(profile)
        with self._connection() as db:
            existing = db.execute(
                "SELECT * FROM documents WHERE user_id=? AND section_id=? AND sha256=?",
                (user_id, section_id, digest),
            ).fetchone()
            if existing is not None:
                document = self._row_document(existing)
                duplicate = True
            else:
                document_id = uuid4().hex
                safe_name = source_path.name.replace("/", "_").replace("\\", "_")
                relative = f"sections/{section_id}/{document_id}-{safe_name}"
                target_dir = self.sections_dir / section_id
                target_dir.mkdir(parents=True, exist_ok=True)
                target = target_dir / f"{document_id}-{safe_name}"
                fd, temporary = tempfile.mkstemp(prefix=".upload-", dir=target_dir)
                os.close(fd)
                try:
                    shutil.copyfile(source_path, temporary)
                    os.replace(temporary, target)
                finally:
                    if os.path.exists(temporary):
                        os.unlink(temporary)
                now = time.time()
                db.execute(
                    "INSERT INTO documents VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        document_id,
                        user_id,
                        section_id,
                        source_path.name,
                        relative,
                        source_path.stat().st_size,
                        digest,
                        "queued",
                        source,
                        now,
                        None,
                    ),
                )
                document = self._row_document(
                    db.execute("SELECT * FROM documents WHERE document_id=?", (document_id,)).fetchone()
                )
                duplicate = False
            now = time.time()
            db.execute(
                "INSERT INTO ingestions VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(document_id, embedding_profile_id) DO UPDATE SET status='queued', updated_at=excluded.updated_at, error=NULL",
                (document.document_id, profile.profile_id, "queued", now, now, None),
            )
        ingestion = DocumentIngestion(document.document_id, profile.profile_id, "queued", now, now)
        return document, ingestion, duplicate

    def index_extraction(
        self,
        document_id: str,
        profile: EmbeddingProfile,
        pages: Iterable[PdfPage],
        *,
        embed: bool = True,
    ) -> list[KnowledgeChunk]:
        profile = self.register_profile(profile)
        with self._connection() as db:
            row = db.execute("SELECT * FROM documents WHERE document_id=?", (document_id,)).fetchone()
            if row is None:
                raise ValueError("document not found")
            document = self._row_document(row)
            now = time.time()
            db.execute(
                "INSERT INTO ingestions VALUES (?, ?, 'queued', ?, ?, NULL) ON CONFLICT(document_id, embedding_profile_id) DO NOTHING",
                (document_id, profile.profile_id, now, now),
            )
            db.execute("UPDATE documents SET status='indexing', error=NULL WHERE document_id=?", (document_id,))
            db.execute(
                "UPDATE ingestions SET status='indexing', updated_at=?, error=NULL WHERE document_id=? AND embedding_profile_id=?",
                (time.time(), document_id, profile.profile_id),
            )
            db.execute(
                "DELETE FROM chunks_fts WHERE rowid IN (SELECT rowid FROM knowledge_chunks WHERE document_id=?)",
                (document_id,),
            )
            db.execute("DELETE FROM knowledge_chunks WHERE document_id=?", (document_id,))
        chunks = chunk_pdf_pages(pages)
        with self._connection() as db:
            for chunk in chunks:
                chunk = KnowledgeChunk(
                    chunk.chunk_id,
                    document_id,
                    document.section_id,
                    chunk.text,
                    chunk.tokenized_text,
                    chunk.page_start,
                    chunk.page_end,
                    chunk.sequence,
                    chunk.token_count,
                    chunk.sha256,
                )
                db.execute(
                    "INSERT INTO knowledge_chunks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        chunk.chunk_id,
                        document.user_id,
                        chunk.section_id,
                        document_id,
                        chunk.text,
                        chunk.tokenized_text,
                        chunk.page_start,
                        chunk.page_end,
                        chunk.sequence,
                        chunk.token_count,
                        chunk.sha256,
                    ),
                )
                db.execute(
                    "INSERT INTO chunks_fts(rowid, text, tokenized_text) SELECT rowid, text, tokenized_text FROM knowledge_chunks WHERE chunk_id=?",
                    (chunk.chunk_id,),
                )
            status = "ready" if (not embed or not chunks) else "ready"
            now = time.time()
            db.execute("UPDATE documents SET status=?, error=NULL WHERE document_id=?", (status, document_id))
            db.execute(
                "UPDATE ingestions SET status=?, updated_at=?, error=NULL WHERE document_id=? AND embedding_profile_id=?",
                (status, now, document_id, profile.profile_id),
            )
        if embed and chunks:
            try:
                self._upsert_vectors(document, profile, chunks)
            except Exception as exc:
                with self._connection() as db:
                    db.execute(
                        "UPDATE ingestions SET status='failed', updated_at=?, error=? WHERE document_id=? AND embedding_profile_id=?",
                        (time.time(), str(exc), document_id, profile.profile_id),
                    )
        return chunks

    def index_document(
        self, document_id: str, profile: EmbeddingProfile, extractor: Any, *, embed: bool = True
    ) -> list[KnowledgeChunk]:
        """Extract and index the managed copy, never the original upload path."""

        with self._connection() as db:
            row = db.execute("SELECT relative_path FROM documents WHERE document_id=?", (document_id,)).fetchone()
        if row is None:
            raise ValueError("document not found")
        relative = str(row["relative_path"])
        managed_path = self.rag_dir / relative
        workspace = getattr(extractor, "_workspace", None)
        if isinstance(workspace, Path):
            path_argument = managed_path.relative_to(workspace).as_posix()
        else:
            path_argument = str(managed_path)
        try:
            result = extractor.extract(path_argument)
            return self.index_extraction(document_id, profile, result.pages, embed=embed)
        except Exception as exc:
            with self._connection() as db:
                db.execute("UPDATE documents SET status='failed', error=? WHERE document_id=?", (str(exc), document_id))
                db.execute(
                    "UPDATE ingestions SET status='failed', updated_at=?, error=? WHERE document_id=? AND embedding_profile_id=?",
                    (time.time(), str(exc), document_id, profile.profile_id),
                )
            raise

    def list_documents(self, *, user_id: str, section_id: str, profile: EmbeddingProfile) -> list[dict[str, Any]]:
        """List files with the status of the selected embedding profile."""

        with self._connection() as db:
            rows = db.execute(
                """SELECT d.*, i.status AS ingestion_status, i.error AS ingestion_error
                   FROM documents d
                   LEFT JOIN ingestions i
                     ON i.document_id=d.document_id AND i.embedding_profile_id=?
                   WHERE d.user_id=? AND d.section_id=?
                   ORDER BY d.created_at DESC""",
                (profile.profile_id, user_id, section_id),
            ).fetchall()
        documents: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            ingestion_status = item.pop("ingestion_status", None)
            ingestion_error = item.pop("ingestion_error", None)
            item["status"] = ingestion_status or "not_imported"
            item["ingestion_status"] = ingestion_status
            item["ingestion_error"] = ingestion_error
            documents.append(item)
        return documents

    def queue_document(
        self,
        document_id: str,
        *,
        user_id: str,
        profile: EmbeddingProfile,
    ) -> tuple[KnowledgeDocument, DocumentIngestion]:
        """Queue an existing managed document for the selected profile."""

        profile = self.register_profile(profile)
        now = time.time()
        with self._connection() as db:
            row = db.execute(
                "SELECT * FROM documents WHERE document_id=? AND user_id=?",
                (document_id, user_id),
            ).fetchone()
            if row is None:
                raise RagNotFoundError("knowledge-base document not found")
            active = db.execute(
                "SELECT 1 FROM ingestions WHERE document_id=? AND status IN ('queued', 'indexing') LIMIT 1",
                (document_id,),
            ).fetchone()
            if active is not None:
                raise RagBusyError("document indexing is already active")
            db.execute(
                """INSERT INTO ingestions VALUES (?, ?, 'queued', ?, ?, NULL)
                   ON CONFLICT(document_id, embedding_profile_id)
                   DO UPDATE SET status='queued', updated_at=excluded.updated_at, error=NULL""",
                (document_id, profile.profile_id, now, now),
            )
            db.execute(
                "UPDATE documents SET status='queued', error=NULL WHERE document_id=?",
                (document_id,),
            )
            document = self._row_document(
                db.execute("SELECT * FROM documents WHERE document_id=?", (document_id,)).fetchone()
            )
        return document, DocumentIngestion(document_id, profile.profile_id, "queued", now, now)

    def delete_document(self, document_id: str, *, user_id: str) -> tuple[KnowledgeDocument, str | None]:
        """Delete a managed copy and authoritative indexes for one user."""

        managed_path: Path | None = None
        staged_path: Path | None = None
        profile_ids: list[str] = []
        document: KnowledgeDocument | None = None
        try:
            with self._connection() as db:
                row = db.execute(
                    "SELECT * FROM documents WHERE document_id=? AND user_id=?",
                    (document_id, user_id),
                ).fetchone()
                if row is None:
                    raise RagNotFoundError("knowledge-base document not found")
                document = self._row_document(row)
                active = db.execute(
                    "SELECT 1 FROM ingestions WHERE document_id=? AND status IN ('queued', 'indexing') LIMIT 1",
                    (document_id,),
                ).fetchone()
                if active is not None:
                    raise RagBusyError("document indexing is active")
                profile_ids = [
                    str(item[0])
                    for item in db.execute(
                        "SELECT embedding_profile_id FROM ingestions WHERE document_id=?",
                        (document_id,),
                    ).fetchall()
                ]
                managed_path = (self.rag_dir / document.relative_path).resolve()
                if not managed_path.is_relative_to(self.sections_dir.resolve()):
                    raise ValueError("managed document path escaped the RAG sections directory")
                if managed_path.exists():
                    if managed_path.is_symlink() or not managed_path.is_file():
                        raise ValueError("managed document is not a regular file")
                    staged_path = managed_path.with_name(f".delete-{uuid4().hex}")
                    os.replace(managed_path, staged_path)
                db.execute(
                    "DELETE FROM chunks_fts WHERE rowid IN (SELECT rowid FROM knowledge_chunks WHERE document_id=?)",
                    (document_id,),
                )
                db.execute("DELETE FROM documents WHERE document_id=?", (document_id,))
        except Exception:
            if staged_path is not None and staged_path.exists() and managed_path is not None:
                os.replace(staged_path, managed_path)
            raise
        if staged_path is not None:
            staged_path.unlink(missing_ok=True)
        assert document is not None
        warnings: list[str] = []
        for profile_id in profile_ids:
            try:
                self._delete_document_vectors(document, profile_id)
            except Exception as exc:
                warnings.append(f"{profile_id}: {exc}")
        warning = "Qdrant cleanup failed: " + "; ".join(warnings) if warnings else None
        return document, warning

    def _delete_document_vectors(self, document: KnowledgeDocument, profile_id: str) -> None:
        response = self.http.post(
            f"{self.qdrant_url}/collections/rag_{profile_id}/points/delete",
            params={"wait": "true"},
            json={
                "filter": {
                    "must": [
                        {"key": "user_id", "match": {"value": document.user_id}},
                        {"key": "document_id", "match": {"value": document.document_id}},
                    ]
                }
            },
            timeout=15,
        )
        if getattr(response, "status_code", 200) == 404:
            return
        response.raise_for_status()

    def requeue_ingestions(self) -> int:
        """Mark derived vector work for regeneration after a snapshot restore."""

        now = time.time()
        with self._connection() as db:
            result = db.execute(
                "UPDATE ingestions SET status='queued', updated_at=?, error=NULL WHERE status != 'queued'",
                (now,),
            )
            db.execute(
                "UPDATE documents SET status='queued', error=NULL WHERE document_id IN (SELECT document_id FROM ingestions)"
            )
        return int(result.rowcount)

    def _upsert_vectors(
        self, document: KnowledgeDocument, profile: EmbeddingProfile, chunks: list[KnowledgeChunk]
    ) -> None:
        collection = f"rag_{profile.profile_id}"
        collection_response = self.http.put(
            f"{self.qdrant_url}/collections/{collection}",
            json={"vectors": {"size": profile.dimension, "distance": "Cosine"}},
            timeout=15,
        )
        if getattr(collection_response, "status_code", 200) != 409:
            collection_response.raise_for_status()
        points = []
        for chunk in chunks:
            vector = self._embed(chunk.text, profile)
            points.append(
                {
                    "id": chunk.chunk_id,
                    "vector": vector,
                    "payload": {
                        "user_id": document.user_id,
                        "section_id": document.section_id,
                        "document_id": document.document_id,
                        "chunk_id": chunk.chunk_id,
                        "embedding_profile_id": profile.profile_id,
                        "page_start": chunk.page_start,
                        "page_end": chunk.page_end,
                    },
                }
            )
        self.http.put(
            f"{self.qdrant_url}/collections/{collection}/points", json={"points": points}, timeout=30
        ).raise_for_status()

    def _bm25(self, query: str, user_id: str, section_id: str, limit: int) -> list[KnowledgeSearchResult]:
        if not query.strip():
            return []
        with self._connection() as db:
            try:
                rows = db.execute(
                    """SELECT c.chunk_id, c.document_id, d.filename, c.text, c.page_start, c.page_end,
                              bm25(chunks_fts) AS score
                       FROM chunks_fts JOIN knowledge_chunks c ON c.rowid=chunks_fts.rowid
                       JOIN documents d ON d.document_id=c.document_id
                       WHERE chunks_fts MATCH ? AND c.user_id=? AND c.section_id=?
                       ORDER BY score LIMIT ?""",
                    (pretokenize(query), user_id, section_id, max(1, min(limit, 100))),
                ).fetchall()
            except sqlite3.OperationalError as exc:
                raise RagDependencyError("SQLite FTS5 is unavailable") from exc
        return [
            KnowledgeSearchResult(
                r["chunk_id"],
                r["document_id"],
                r["filename"],
                r["text"],
                r["page_start"],
                r["page_end"],
                float(-r["score"]),
                "bm25",
                i + 1,
            )
            for i, r in enumerate(rows)
        ]

    def _embed(self, query: str, profile: EmbeddingProfile) -> list[float]:
        response = self.http.post(
            f"{profile.base_url.rstrip('/')}/api/embed", json={"model": profile.model, "input": query}, timeout=15
        )
        response.raise_for_status()
        payload = response.json()
        embeddings = payload.get("embeddings") if isinstance(payload, dict) else None
        if isinstance(embeddings, list) and embeddings and isinstance(embeddings[0], list):
            vector = embeddings[0]
        else:
            vector = payload.get("embedding") if isinstance(payload, dict) else None
        if not isinstance(vector, list) or not vector:
            raise RagDependencyError("Ollama returned no embedding")
        return [float(value) for value in vector]

    def _vector(
        self, query: str, user_id: str, section_id: str, profile: EmbeddingProfile, limit: int
    ) -> list[KnowledgeSearchResult]:
        vector = self._embed(query, profile)
        collection = f"rag_{profile.profile_id}"
        response = self.http.post(
            f"{self.qdrant_url}/collections/{collection}/points/search",
            json={
                "vector": vector,
                "limit": max(1, min(limit, 100)),
                "with_payload": True,
                "filter": {
                    "must": [
                        {"key": "user_id", "match": {"value": user_id}},
                        {"key": "section_id", "match": {"value": section_id}},
                        {"key": "embedding_profile_id", "match": {"value": profile.profile_id}},
                    ]
                },
            },
            timeout=15,
        )
        response.raise_for_status()
        payload = response.json()
        hits = payload.get("result", []) if isinstance(payload, dict) else []
        results: list[KnowledgeSearchResult] = []
        with self._connection() as db:
            for index, hit in enumerate(hits):
                point = hit.get("payload", {}) if isinstance(hit, dict) else {}
                chunk_id = str(point.get("chunk_id") or hit.get("id"))
                row = db.execute(
                    "SELECT c.*, d.filename FROM knowledge_chunks c JOIN documents d ON d.document_id=c.document_id WHERE c.chunk_id=? AND c.user_id=? AND c.section_id=?",
                    (chunk_id, user_id, section_id),
                ).fetchone()
                if row is None:
                    continue
                results.append(
                    KnowledgeSearchResult(
                        chunk_id,
                        row["document_id"],
                        row["filename"],
                        row["text"],
                        row["page_start"],
                        row["page_end"],
                        float(hit.get("score", 0.0)),
                        "vector",
                        index + 1,
                    )
                )
        return results

    def search(
        self,
        query: str,
        *,
        user_id: str,
        section_id: str,
        profile: EmbeddingProfile,
        algorithm: RagAlgorithm = "hybrid",
        bm25_candidate_k: int = 20,
        vector_candidate_k: int = 20,
        top_k: int = 8,
    ) -> KnowledgeSearchResponse:
        top_k = max(1, min(top_k, 20))
        warning: str | None = None
        bm25 = self._bm25(query, user_id, section_id, bm25_candidate_k) if algorithm in {"bm25", "hybrid"} else []
        vector: list[KnowledgeSearchResult] = []
        if algorithm in {"vector", "hybrid"}:
            try:
                vector = self._vector(query, user_id, section_id, profile, vector_candidate_k)
            except Exception as exc:
                if algorithm == "vector":
                    raise RagDependencyError(f"Vector retrieval unavailable: {exc}") from exc
                warning = f"向量检索暂不可用，已降级为 BM25：{exc}"
        if algorithm == "bm25" or (algorithm == "hybrid" and not vector):
            return KnowledgeSearchResponse(
                tuple(bm25[:top_k]), "bm25" if algorithm == "hybrid" and warning else algorithm, warning
            )
        if algorithm == "vector":
            return KnowledgeSearchResponse(tuple(vector[:top_k]), "vector", warning)
        by_id: dict[str, KnowledgeSearchResult] = {}
        scores: dict[str, float] = {}
        for values in (bm25, vector):
            for item in values:
                by_id[item.chunk_id] = item
                scores[item.chunk_id] = scores.get(item.chunk_id, 0.0) + 1.0 / (RRF_K + item.rank)
        ordered = sorted(by_id.values(), key=lambda item: scores[item.chunk_id], reverse=True)[:top_k]
        final = tuple(
            KnowledgeSearchResult(
                item.chunk_id,
                item.document_id,
                item.filename,
                item.text,
                item.page_start,
                item.page_end,
                scores[item.chunk_id],
                "hybrid",
                index + 1,
            )
            for index, item in enumerate(ordered)
        )
        return KnowledgeSearchResponse(final, "hybrid", warning)

    def capabilities(self, profile: EmbeddingProfile) -> dict[str, Any]:
        fts5 = True
        try:
            with self._connection() as db:
                db.execute("SELECT count(*) FROM chunks_fts").fetchone()
        except sqlite3.OperationalError:
            fts5 = False
        qdrant = False
        try:
            qdrant = self.http.get(f"{self.qdrant_url}/healthz", timeout=2).ok
        except requests.RequestException:
            pass
        ollama = False
        models: list[str] = []
        try:
            response = self.http.get(f"{profile.base_url.rstrip('/')}/api/tags", timeout=2)
            if response.ok:
                ollama = True
                values = response.json().get("models", [])
                models = [str(item.get("name")) for item in values if isinstance(item, dict) and item.get("name")]
        except (requests.RequestException, ValueError):
            pass
        with self._connection() as db:
            imported = int(
                db.execute(
                    "SELECT count(*) FROM ingestions WHERE embedding_profile_id=? AND status='ready'",
                    (profile.profile_id,),
                ).fetchone()[0]
            )
        algorithms = (["bm25"] if fts5 else []) + (["vector"] if ollama and qdrant else [])
        if fts5 and ollama and qdrant:
            algorithms.append("hybrid")
        return {
            "fts5_available": fts5,
            "qdrant_healthy": qdrant,
            "ollama_healthy": ollama,
            "embedding_models": models,
            "algorithms": algorithms,
            "profile_id": profile.profile_id,
            "dimension": profile.dimension,
            "imported_files": imported,
        }


__all__ = ["KnowledgeBaseService", "RagDependencyError", "RRF_K"]
