"""Versioned PostgreSQL schema for Web identities and authentication state."""

from __future__ import annotations

AUTH_SCHEMA_VERSION = 4

AUTH_SCHEMA_STATEMENTS = (
    """CREATE TABLE IF NOT EXISTS web_auth_schema_migrations (
        version INTEGER PRIMARY KEY,
        applied_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
    )""",
    """CREATE TABLE IF NOT EXISTS users (
        id TEXT PRIMARY KEY,
        email TEXT UNIQUE,
        password_hash TEXT NOT NULL DEFAULT '',
        created_at DOUBLE PRECISION NOT NULL,
        kind TEXT NOT NULL DEFAULT 'account'
    )""",
    """ALTER TABLE users ADD COLUMN IF NOT EXISTS kind TEXT NOT NULL DEFAULT 'account'""",
    """ALTER TABLE users ALTER COLUMN email DROP NOT NULL""",
    """CREATE TABLE IF NOT EXISTS verification_challenges (
        id TEXT PRIMARY KEY,
        email TEXT NOT NULL,
        purpose TEXT NOT NULL,
        code_hash TEXT NOT NULL,
        ip_address TEXT,
        created_at DOUBLE PRECISION NOT NULL,
        expires_at DOUBLE PRECISION NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0,
        consumed_at DOUBLE PRECISION
    )""",
    """CREATE INDEX IF NOT EXISTS verification_email_purpose_idx
        ON verification_challenges(email, purpose, created_at DESC)""",
    """CREATE TABLE IF NOT EXISTS auth_sessions (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        kind TEXT NOT NULL,
        token_hash TEXT NOT NULL UNIQUE,
        created_at DOUBLE PRECISION NOT NULL,
        expires_at DOUBLE PRECISION NOT NULL,
        last_seen_at DOUBLE PRECISION NOT NULL,
        revoked_at DOUBLE PRECISION
    )""",
    """CREATE INDEX IF NOT EXISTS auth_sessions_lookup_idx
        ON auth_sessions(token_hash, expires_at, revoked_at)""",
    """CREATE INDEX IF NOT EXISTS auth_sessions_user_idx
        ON auth_sessions(user_id, revoked_at)""",
    """CREATE TABLE IF NOT EXISTS guest_imports (
        target_user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
        guest_user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        status TEXT NOT NULL DEFAULT 'pending',
        created_at DOUBLE PRECISION NOT NULL,
        updated_at DOUBLE PRECISION NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS device_grants (
        id TEXT PRIMARY KEY,
        poll_hash TEXT NOT NULL UNIQUE,
        browser_hash TEXT NOT NULL UNIQUE,
        server_url TEXT NOT NULL,
        created_at DOUBLE PRECISION NOT NULL,
        expires_at DOUBLE PRECISION NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        user_id TEXT REFERENCES users(id) ON DELETE CASCADE,
        approved_at DOUBLE PRECISION,
        consumed_at DOUBLE PRECISION
    )""",
    """CREATE INDEX IF NOT EXISTS device_grants_poll_idx
        ON device_grants(poll_hash, expires_at, status)""",
    """CREATE INDEX IF NOT EXISTS device_grants_browser_idx
        ON device_grants(browser_hash, expires_at, status)""",
    """CREATE TABLE IF NOT EXISTS rate_limits (
        key TEXT NOT NULL,
        action TEXT NOT NULL,
        window_start BIGINT NOT NULL,
        count INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(key, action, window_start)
    )""",
    """CREATE TABLE IF NOT EXISTS app_metadata (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )""",
)
