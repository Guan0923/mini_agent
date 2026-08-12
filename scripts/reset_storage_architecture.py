"""Verify the local-first storage migration without deleting user data.

The old Web deployment used a destructive reset command to clear server-owned
tables and local roots.  The four-layer architecture keeps formal accounts,
UUIDs, cloud key envelopes and snapshot metadata in place, so an upgrade is a
read-only compatibility check.  Cloud startup applies only additive schema
migrations; this script reports what is present and never modifies PostgreSQL
or the filesystem.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from uuid import UUID

REQUIRED_CLOUD_TABLES = frozenset(
    {
        "users",
        "auth_sessions",
        "verification_challenges",
        "device_grants",
        "rate_limits",
        "guest_imports",
        "cloud_user_keys",
        "cloud_snapshots",
        "cloud_snapshot_chunks",
    }
)


def existing_tables(connection) -> set[str]:
    """Return tables in the active PostgreSQL schema without changing it."""

    rows = connection.execute("SELECT tablename FROM pg_tables WHERE schemaname = current_schema()").fetchall()
    return {str(row[0]) for row in rows}


def account_rows(connection) -> list[dict[str, object]]:
    """Read formal accounts for the migration report."""

    if "users" not in existing_tables(connection):
        return []
    rows = connection.execute(
        "SELECT id,email,created_at FROM users WHERE kind='account' ORDER BY created_at,id"
    ).fetchall()
    return [
        {
            "id": str(row[0]),
            "email": str(row[1]),
            "created_at": row[2].isoformat() if hasattr(row[2], "isoformat") else row[2],
        }
        for row in rows
    ]


def canonical_account_ids(connection) -> dict[str, str]:
    """Validate that formal account IDs already use canonical UUID spelling."""

    if "users" not in existing_tables(connection):
        return {}
    rows = connection.execute("SELECT id FROM users WHERE kind='account' ORDER BY id").fetchall()
    result: dict[str, str] = {}
    for row in rows:
        value = str(row[0])
        try:
            canonical = str(UUID(value))
        except (ValueError, AttributeError) as exc:
            raise RuntimeError(f"Formal account ID is not a UUID: {value}") from exc
        if canonical != value:
            raise RuntimeError(f"Formal account ID is not canonical: {value} -> {canonical}")
        result[value] = canonical
    return result


def local_account_directories(data_root: Path) -> list[str]:
    """List existing canonical user directories without creating or moving them."""

    if not data_root.exists():
        return []
    if data_root.is_symlink() or not data_root.is_dir():
        raise RuntimeError(f"Local data root must be a regular directory: {data_root}")
    users: list[str] = []
    for child in sorted(data_root.iterdir(), key=lambda item: item.name):
        if child.is_symlink() or not child.is_dir():
            continue
        try:
            value = str(UUID(child.name))
        except (ValueError, AttributeError):
            continue
        if value != child.name:
            raise RuntimeError(f"Local account directory is not canonical: {child.name} -> {value}")
        users.append(value)
    return users


def inspect_cloud(database_url: str) -> dict[str, object]:
    """Inspect cloud schema and accounts using a short-lived read-only connection."""

    import psycopg

    with psycopg.connect(database_url, connect_timeout=10) as connection:
        connection.execute("SET TRANSACTION READ ONLY")
        tables = existing_tables(connection)
        accounts = account_rows(connection)
        canonical_account_ids(connection)
    missing = sorted(REQUIRED_CLOUD_TABLES - tables)
    return {
        "database_checked": True,
        "cloud_tables": sorted(tables & REQUIRED_CLOUD_TABLES),
        "missing_cloud_tables": missing,
        "accounts_preserved": len(accounts),
        "account_ids_canonical": not missing,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, default=Path.home() / ".mini_agent")
    parser.add_argument("--database-url", default=os.environ.get("DATABASE_URL", ""))
    args = parser.parse_args()

    data_root = args.data_root.expanduser().resolve()
    if data_root.is_symlink():
        raise SystemExit("Refusing to inspect a symbolic-link data root.")
    local_users = local_account_directories(data_root)
    report: dict[str, object] = {
        "status": "ready",
        "destructive_actions": False,
        "data_root": str(data_root),
        "local_account_directories": local_users,
        "local_accounts_reused": len(local_users),
    }
    if args.database_url:
        try:
            report.update(inspect_cloud(args.database_url))
        except Exception as exc:
            report.update(
                {
                    "status": "cloud_unavailable",
                    "database_checked": True,
                    "cloud_error": f"{type(exc).__name__}: {exc}",
                }
            )
    else:
        report.update({"database_checked": False, "cloud_note": "DATABASE_URL not supplied; cloud check skipped."})
    if report.get("missing_cloud_tables"):
        report["status"] = "cloud_schema_incomplete"
        report["cloud_note"] = "Start the cloud service to apply additive schema migrations."
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return 0 if report["status"] == "ready" else 1


if __name__ == "__main__":
    raise SystemExit(main())
