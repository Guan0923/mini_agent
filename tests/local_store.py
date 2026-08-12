"""Small helpers for tests that exercise the local-first SQLite store."""

from __future__ import annotations

from pathlib import Path

from backend.configuration import ClientPaths
from backend.storage.sqlite import SQLiteSessionStore


def session_store(root: Path, device_id: str = "device_test") -> SQLiteSessionStore:
    """Create an isolated local session store below a pytest temp directory."""

    return SQLiteSessionStore(ClientPaths(Path(root)), device_id)


__all__ = ["session_store"]
