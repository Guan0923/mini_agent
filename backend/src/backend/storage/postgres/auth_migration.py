"""Explicit, auditable migration from the legacy Web SQLite auth store."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
from pathlib import Path
from typing import Any

from psycopg.types.json import Jsonb

from .auth import PostgresAuthRepository
from .settings import PostgresSettingsRepository

_REQUIRED_TABLES = {
    "users",
    "user_profiles",
    "user_agent_settings",
    "user_provider_settings",
    "user_capability_settings",
    "app_metadata",
}
_MIGRATION_LOCK = "mini-agent:web-auth-data-migration:v1"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _rows(connection: sqlite3.Connection, table: str) -> list[dict[str, Any]]:
    return [dict(row) for row in connection.execute(f"SELECT * FROM {table}").fetchall()]


def _read_source(path: Path) -> tuple[str, dict[str, list[dict[str, Any]]]]:
    resolved = path.resolve(strict=True)
    wal_path = resolved.with_name(f"{resolved.name}-wal")
    if wal_path.exists() and wal_path.stat().st_size:
        raise ValueError("SQLite WAL is not empty; stop the old Web backend and checkpoint the database first.")
    before = _sha256(resolved)
    uri = f"file:{resolved.as_posix()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        connection.row_factory = sqlite3.Row
        tables = {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        missing = sorted(_REQUIRED_TABLES - tables)
        if missing:
            raise ValueError(f"Source auth database is missing required tables: {', '.join(missing)}")
        snapshot = {table: _rows(connection, table) for table in sorted(_REQUIRED_TABLES)}
    after = _sha256(resolved)
    if before != after:
        raise ValueError("Source auth database changed during migration preflight.")
    snapshot["app_metadata"] = [
        row for row in snapshot["app_metadata"] if str(row.get("key", "")).startswith("legacy_migration:")
    ]
    user_ids = {str(row["id"]) for row in snapshot["users"]}
    for table in sorted(_REQUIRED_TABLES - {"users", "app_metadata"}):
        orphan_count = sum(str(row.get("user_id", "")) not in user_ids for row in snapshot[table])
        if orphan_count:
            raise ValueError(f"Source auth database contains {orphan_count} orphaned row(s) in {table}.")
    return before, snapshot


def _counts(snapshot: dict[str, list[dict[str, Any]]]) -> dict[str, int]:
    return {table: len(rows) for table, rows in snapshot.items()}


def _migration_applied(connection, source_sha256: str) -> bool:
    return (
        connection.execute("SELECT 1 FROM web_auth_data_migrations WHERE source_sha256=%s", (source_sha256,)).fetchone()
        is not None
    )


def _validate_conflicts(connection, users: list[dict[str, Any]]) -> None:
    if not users:
        return
    ids = [str(row["id"]) for row in users]
    emails = [str(row["email"]) for row in users]
    target_rows = connection.execute(
        "SELECT id,email,password_hash FROM users WHERE id=ANY(%s) OR email=ANY(%s)", (ids, emails)
    ).fetchall()
    source_by_id = {str(row["id"]): row for row in users}
    source_by_email = {str(row["email"]): row for row in users}
    conflicts = 0
    for target in target_rows:
        source = source_by_id.get(str(target["id"])) or source_by_email.get(str(target["email"]))
        if source is None or any(
            (
                str(target["id"]) != str(source["id"]),
                str(target["email"]) != str(source["email"]),
                str(target["password_hash"]) != str(source["password_hash"]),
            )
        ):
            conflicts += 1
    if conflicts:
        raise ValueError(f"Target PostgreSQL contains {conflicts} conflicting user record(s).")


def check_migration(source: Path, auth: PostgresAuthRepository) -> dict[str, object]:
    source_sha256, snapshot = _read_source(source)
    with auth.connection() as connection:
        applied = _migration_applied(connection, source_sha256)
        _validate_conflicts(connection, snapshot["users"])
    return {
        "status": "already_applied" if applied else "ready",
        "source_sha256": source_sha256,
        "counts": _counts(snapshot),
        "provider_api_keys": "will_be_cleared",
        "sessions": "will_not_be_migrated",
    }


def _upsert_settings(connection, snapshot: dict[str, list[dict[str, Any]]]) -> None:
    for row in snapshot["user_profiles"]:
        connection.execute(
            """INSERT INTO user_profiles(user_id,display_name,agent_preferences,updated_at)
            VALUES (%s,%s,%s,%s) ON CONFLICT(user_id) DO UPDATE SET
            display_name=EXCLUDED.display_name,agent_preferences=EXCLUDED.agent_preferences,
            updated_at=EXCLUDED.updated_at""",
            (row["user_id"], row["display_name"], row["agent_preferences"], row["updated_at"]),
        )
    for row in snapshot["user_agent_settings"]:
        connection.execute(
            """INSERT INTO user_agent_settings
            (user_id,tone,verbosity,initiative,custom_instructions,display_mode,timezone,location_enabled,updated_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(user_id) DO UPDATE SET
            tone=EXCLUDED.tone,verbosity=EXCLUDED.verbosity,initiative=EXCLUDED.initiative,
            custom_instructions=EXCLUDED.custom_instructions,display_mode=EXCLUDED.display_mode,
            timezone=EXCLUDED.timezone,location_enabled=EXCLUDED.location_enabled,updated_at=EXCLUDED.updated_at""",
            (
                row["user_id"],
                row["tone"],
                row["verbosity"],
                row["initiative"],
                row["custom_instructions"],
                row.get("display_mode", "medium"),
                row.get("timezone", "Asia/Shanghai"),
                bool(row.get("location_enabled", False)),
                row["updated_at"],
            ),
        )
    for row in snapshot["user_provider_settings"]:
        connection.execute(
            """INSERT INTO user_provider_settings
            (user_id,provider,protocol,base_url,model,max_tokens,context_size,tokenizer_model,
             api_key_ciphertext,updated_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'',%s) ON CONFLICT(user_id) DO UPDATE SET
            provider=EXCLUDED.provider,protocol=EXCLUDED.protocol,base_url=EXCLUDED.base_url,
            model=EXCLUDED.model,max_tokens=EXCLUDED.max_tokens,context_size=EXCLUDED.context_size,
            tokenizer_model=EXCLUDED.tokenizer_model,api_key_ciphertext='',updated_at=EXCLUDED.updated_at""",
            (
                row["user_id"],
                row["provider"],
                row.get("protocol", "chat_completions"),
                row["base_url"],
                row["model"],
                row["max_tokens"],
                row["context_size"],
                row["tokenizer_model"],
                row["updated_at"],
            ),
        )
    for row in snapshot["user_capability_settings"]:
        try:
            value = json.loads(str(row.get("settings_json") or "{}"))
        except json.JSONDecodeError as exc:
            raise ValueError("Source capability settings contain invalid JSON.") from exc
        connection.execute(
            """INSERT INTO user_capability_settings(user_id,settings_json,updated_at)
            VALUES (%s,%s,%s) ON CONFLICT(user_id) DO UPDATE SET
            settings_json=EXCLUDED.settings_json,updated_at=EXCLUDED.updated_at""",
            (row["user_id"], Jsonb(value), row["updated_at"]),
        )
    for row in snapshot["app_metadata"]:
        connection.execute(
            """INSERT INTO app_metadata(key,value) VALUES (%s,%s)
            ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value""",
            (row["key"], row["value"]),
        )


def apply_migration(source: Path, auth: PostgresAuthRepository) -> dict[str, object]:
    source_sha256, snapshot = _read_source(source)
    counts = _counts(snapshot)
    with auth.connection() as connection:
        connection.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (_MIGRATION_LOCK,))
        if _migration_applied(connection, source_sha256):
            return {
                "status": "already_applied",
                "source_sha256": source_sha256,
                "counts": counts,
            }
        _validate_conflicts(connection, snapshot["users"])
        for row in snapshot["users"]:
            connection.execute(
                """INSERT INTO users(id,email,password_hash,created_at,legacy_owner)
                VALUES (%s,%s,%s,%s,%s) ON CONFLICT(id) DO NOTHING""",
                (row["id"], row["email"], row["password_hash"], row["created_at"], bool(row["legacy_owner"])),
            )
        _upsert_settings(connection, snapshot)
        connection.execute(
            """INSERT INTO web_auth_data_migrations(source_sha256,source_path,counts_json)
            VALUES (%s,%s,%s)""",
            (source_sha256, str(source.resolve()), Jsonb(counts)),
        )
    return {"status": "applied", "source_sha256": source_sha256, "counts": counts}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Migrate legacy Web auth data from SQLite to PostgreSQL.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("check", "apply"):
        child = subparsers.add_parser(command)
        child.add_argument("--source", type=Path, required=True, help="Path to the legacy auth.sqlite3 file")
    return parser


def main() -> None:
    parser = _parser()
    args = parser.parse_args()
    database_url = os.environ.get("DATABASE_URL", "").strip()
    secret_key = os.environ.get("MINI_AGENT_SECRET_KEY", "")
    if not database_url:
        parser.error("DATABASE_URL is required")
    if not secret_key:
        parser.error("MINI_AGENT_SECRET_KEY is required")
    try:
        auth = PostgresAuthRepository(database_url)
        PostgresSettingsRepository(database_url, secret_key=secret_key)
        result = check_migration(args.source, auth) if args.command == "check" else apply_migration(args.source, auth)
    except (OSError, RuntimeError, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
