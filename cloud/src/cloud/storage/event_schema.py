"""PostgreSQL schema for encrypted event batches and revision heads."""

from __future__ import annotations

EVENT_SCHEMA_VERSION = 1

EVENT_SCHEMA_STATEMENTS = (
    """CREATE TABLE IF NOT EXISTS cloud_event_schema_migrations (
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
    """CREATE TABLE IF NOT EXISTS cloud_event_heads (
        user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        session_id TEXT NOT NULL,
        head_revision BIGINT NOT NULL DEFAULT 0,
        baseline_batch_id TEXT,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY(user_id, session_id)
    )""",
    """CREATE TABLE IF NOT EXISTS cloud_event_batches (
        user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        session_id TEXT NOT NULL,
        revision BIGINT NOT NULL,
        batch_id TEXT NOT NULL,
        parent_revision BIGINT NOT NULL,
        device_id TEXT NOT NULL,
        nonce TEXT NOT NULL,
        envelope_json JSONB NOT NULL,
        checksum TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY(user_id, session_id, revision),
        UNIQUE(user_id, session_id, batch_id)
    )""",
    """CREATE TABLE IF NOT EXISTS cloud_event_ids (
        user_id TEXT NOT NULL,
        session_id TEXT NOT NULL,
        event_id TEXT NOT NULL,
        revision BIGINT NOT NULL,
        batch_id TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY(user_id, session_id, event_id),
        UNIQUE(user_id, session_id, revision, event_id)
    )""",
    """CREATE INDEX IF NOT EXISTS cloud_event_batches_pull_idx
        ON cloud_event_batches(user_id, session_id, revision)""",
)

__all__ = ["EVENT_SCHEMA_STATEMENTS", "EVENT_SCHEMA_VERSION"]
