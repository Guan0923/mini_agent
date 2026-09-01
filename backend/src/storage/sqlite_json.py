"""Shared SQLite JSON-object read primitives."""

from __future__ import annotations

import json
import sqlite3


def read_json_object(
    connection: sqlite3.Connection,
    session_id: str,
    namespace: str,
    object_id: str,
) -> dict[str, object] | None:
    """Return one object payload, rejecting non-object JSON values."""

    row = connection.execute(
        "SELECT payload_json FROM json_objects WHERE session_id=? AND namespace=? AND object_id=?",
        (session_id, namespace, object_id),
    ).fetchone()
    if row is None:
        return None
    value = json.loads(str(row[0]))
    return dict(value) if isinstance(value, dict) else None
