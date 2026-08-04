"""PostgreSQL-backed synchronization API for remote deployment."""

from __future__ import annotations

import json
import secrets
from collections.abc import Mapping
from typing import Any


class RevisionConflict(ValueError):
    pass


class SessionOwnershipError(PermissionError):
    pass


class PostgresSyncRepository:
    def __init__(self, database_url: str) -> None:
        self.database_url = database_url
        self.initialize()

    def _connect(self):
        import psycopg

        return psycopg.connect(self.database_url, connect_timeout=10)

    def initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS synced_sessions (
                session_id TEXT PRIMARY KEY, owner_device_id TEXT NOT NULL, revision BIGINT NOT NULL,
                snapshot_json JSONB NOT NULL, updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP)"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS sync_operations (
                operation_id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES synced_sessions(session_id),
                device_id TEXT NOT NULL, resulting_revision BIGINT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP)"""
            )

    def push(self, device_id: str, operations: list[dict[str, Any]]) -> list[dict[str, object]]:
        acknowledged: list[dict[str, object]] = []
        with self._connect() as connection:
            from psycopg.types.json import Jsonb

            for operation in operations:
                operation_id = _required_text(operation, "operation_id")
                session_id = _required_text(operation, "session_id")
                base_revision = _base_revision(operation)
                snapshot = operation.get("snapshot")
                if operation.get("kind", "snapshot") != "snapshot":
                    raise ValueError("kind must be snapshot")
                if not isinstance(snapshot, dict):
                    raise ValueError("snapshot must be an object")
                session_meta = snapshot.get("session")
                if not isinstance(session_meta, dict) or session_meta.get("session_id") != session_id:
                    raise ValueError("snapshot session id must match the operation")
                connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s))",
                    (session_id,),
                )
                previous = connection.execute(
                    "SELECT session_id,device_id,resulting_revision FROM sync_operations WHERE operation_id=%s",
                    (operation_id,),
                ).fetchone()
                if previous is not None:
                    if str(previous[0]) != session_id:
                        raise ValueError("operation_id belongs to another session")
                    if str(previous[1]) != device_id:
                        raise SessionOwnershipError(session_id)
                    acknowledged.append({"operation_id": operation_id, "revision": int(previous[2])})
                    continue
                row = connection.execute(
                    "SELECT owner_device_id, revision FROM synced_sessions WHERE session_id=%s FOR UPDATE",
                    (session_id,),
                ).fetchone()
                if row is None:
                    if base_revision != 0:
                        raise RevisionConflict(session_id)
                    revision = 1
                    connection.execute(
                        "INSERT INTO synced_sessions VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP)",
                        (session_id, device_id, revision, Jsonb(snapshot)),
                    )
                else:
                    if str(row[0]) != device_id:
                        raise SessionOwnershipError(session_id)
                    if int(row[1]) != base_revision:
                        raise RevisionConflict(session_id)
                    revision = base_revision + 1
                    connection.execute(
                        "UPDATE synced_sessions SET revision=%s,snapshot_json=%s,updated_at=CURRENT_TIMESTAMP WHERE session_id=%s",
                        (revision, Jsonb(snapshot), session_id),
                    )
                connection.execute(
                    "INSERT INTO sync_operations(operation_id,session_id,device_id,resulting_revision) VALUES (%s,%s,%s,%s)",
                    (operation_id, session_id, device_id, revision),
                )
                acknowledged.append({"operation_id": operation_id, "revision": revision})
        return acknowledged

    def pull(self, known: Mapping[str, object]) -> list[dict[str, object]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT session_id,owner_device_id,revision,snapshot_json FROM synced_sessions ORDER BY updated_at"
            ).fetchall()
        result: list[dict[str, object]] = []
        for session_id, owner_device_id, revision, snapshot in rows:
            if int(revision) <= int(known.get(str(session_id), 0)):
                continue
            value = snapshot if isinstance(snapshot, dict) else json.loads(str(snapshot))
            result.append(
                {
                    "session_id": str(session_id),
                    "owner_device_id": str(owner_device_id),
                    "revision": int(revision),
                    "snapshot": value,
                }
            )
        return result


def create_sync_app(repository: PostgresSyncRepository, bearer_token: str):
    from fastapi import FastAPI, Header, HTTPException

    app = FastAPI(title="Mini-Agent Sync", docs_url=None, redoc_url=None)
    if not bearer_token:
        raise ValueError("bearer_token is required")

    def authenticate(authorization: str | None, device_id: str | None) -> str:
        expected = f"Bearer {bearer_token}"
        if authorization is None or not secrets.compare_digest(authorization, expected):
            raise HTTPException(status_code=401, detail="Unauthorized")
        if not device_id:
            raise HTTPException(status_code=400, detail="X-Device-ID is required")
        return device_id

    @app.get("/healthz")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/sync/push")
    def push(
        payload: dict[str, object], authorization: str | None = Header(None), x_device_id: str | None = Header(None)
    ):
        device_id = authenticate(authorization, x_device_id)
        operations = payload.get("operations", [])
        if not isinstance(operations, list) or not all(isinstance(item, dict) for item in operations):
            raise HTTPException(status_code=422, detail="operations must be objects")
        try:
            return {"acknowledged": repository.push(device_id, operations)}
        except SessionOwnershipError as exc:
            raise HTTPException(status_code=403, detail="Session is owned by another device") from exc
        except RevisionConflict as exc:
            raise HTTPException(status_code=409, detail="Revision conflict") from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="Invalid sync operation") from exc

    @app.post("/v1/sync/pull")
    def pull(
        payload: dict[str, object], authorization: str | None = Header(None), x_device_id: str | None = Header(None)
    ):
        authenticate(authorization, x_device_id)
        known = payload.get("known", {})
        if not isinstance(known, dict):
            raise HTTPException(status_code=422, detail="known must be an object")
        try:
            return {"sessions": repository.pull(known)}
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail="known revisions must be integers") from exc

    return app


def _required_text(value: Mapping[str, object], name: str) -> str:
    item = value.get(name)
    if not isinstance(item, str) or not item:
        raise ValueError(f"{name} is required")
    return item


def _base_revision(value: Mapping[str, object]) -> int:
    try:
        revision = int(value.get("base_revision", 0))
    except (TypeError, ValueError) as exc:
        raise ValueError("base_revision must be an integer") from exc
    if revision < 0:
        raise ValueError("base_revision cannot be negative")
    return revision
