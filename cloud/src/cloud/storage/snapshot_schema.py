"""Versioned PostgreSQL schema owned by the cloud snapshot service."""

from __future__ import annotations

SNAPSHOT_SCHEMA_VERSION = 1

SNAPSHOT_SCHEMA_STATEMENTS = (
    """CREATE TABLE IF NOT EXISTS cloud_sync_schema_migrations (
        version INTEGER PRIMARY KEY,
        applied_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
    )""",
    """CREATE TABLE IF NOT EXISTS cloud_user_keys (
        user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
        wrapped_dek BYTEA NOT NULL,
        nonce BYTEA NOT NULL,
        master_key_version TEXT NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
    )""",
    """CREATE TABLE IF NOT EXISTS cloud_snapshots (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        version BIGINT NOT NULL,
        parent_snapshot_id TEXT,
        status TEXT NOT NULL,
        local_revision BIGINT NOT NULL,
        device_id TEXT NOT NULL,
        archive_sha256 TEXT NOT NULL DEFAULT '',
        archive_size BIGINT NOT NULL DEFAULT 0,
        chunk_count INTEGER NOT NULL DEFAULT 0,
        created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        completed_at TIMESTAMPTZ,
        UNIQUE(user_id, version)
    )""",
    """CREATE TABLE IF NOT EXISTS cloud_snapshot_chunks (
        snapshot_id TEXT NOT NULL REFERENCES cloud_snapshots(id) ON DELETE CASCADE,
        sequence INTEGER NOT NULL,
        nonce BYTEA NOT NULL,
        ciphertext BYTEA NOT NULL,
        checksum TEXT NOT NULL,
        PRIMARY KEY(snapshot_id, sequence)
    )""",
    """CREATE INDEX IF NOT EXISTS cloud_snapshots_user_idx
        ON cloud_snapshots(user_id, version DESC)""",
    """CREATE INDEX IF NOT EXISTS cloud_snapshot_chunks_snapshot_idx
        ON cloud_snapshot_chunks(snapshot_id, sequence)""",
)


__all__ = ["SNAPSHOT_SCHEMA_STATEMENTS", "SNAPSHOT_SCHEMA_VERSION"]
