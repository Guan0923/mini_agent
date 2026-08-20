"""Versioned PostgreSQL schema owned by encrypted event synchronization."""

from __future__ import annotations

EVENT_SCHEMA_VERSION = 3

EVENT_SCHEMA_STATEMENTS = (
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
    # v2 event synchronization.  The envelope is JSON metadata plus an
    # authenticated ciphertext string; the cloud never receives plaintext
    # messages or state fields.
    """CREATE TABLE IF NOT EXISTS cloud_sync_sessions (
        user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        session_id TEXT NOT NULL,
        head_revision BIGINT NOT NULL DEFAULT 0,
        baseline_event_id TEXT,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY(user_id, session_id)
    )""",
    """CREATE TABLE IF NOT EXISTS cloud_sync_events (
        user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        session_id TEXT NOT NULL,
        revision BIGINT NOT NULL,
        event_id TEXT NOT NULL,
        parent_revision BIGINT NOT NULL,
        device_id TEXT NOT NULL,
        envelope_json JSONB NOT NULL,
        checksum TEXT NOT NULL,
        event_ids_json JSONB NOT NULL DEFAULT '[]'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY(user_id, session_id, revision),
        UNIQUE(user_id, session_id, event_id)
    )""",
    """ALTER TABLE cloud_sync_events
        ADD COLUMN IF NOT EXISTS event_ids_json JSONB NOT NULL DEFAULT '[]'::jsonb""",
    """CREATE INDEX IF NOT EXISTS cloud_sync_events_pull_idx
        ON cloud_sync_events(user_id, session_id, revision)""",
)


__all__ = ["EVENT_SCHEMA_STATEMENTS", "EVENT_SCHEMA_VERSION"]
