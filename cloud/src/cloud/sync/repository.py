"""PostgreSQL metadata and encrypted event-batch storage."""

from __future__ import annotations

import json

from cloud.storage.crypto import CloudMasterCipher
from cloud.storage.snapshot_schema import EVENT_SCHEMA_STATEMENTS, EVENT_SCHEMA_VERSION


class CloudSyncConflict(RuntimeError):
    """The local client is not based on the current cloud head."""


class PostgresCloudEventRepository:
    """Keep encrypted event batches in PostgreSQL; plaintext never crosses this port."""

    def __init__(self, database_url: str, *, master_cipher: CloudMasterCipher | None = None) -> None:
        self.database_url = database_url
        self.master_cipher = master_cipher or CloudMasterCipher()
        self.initialize()

    def _connect(self):
        import psycopg

        return psycopg.connect(self.database_url, connect_timeout=10)

    def initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(EVENT_SCHEMA_STATEMENTS[0])
            row = connection.execute("SELECT MAX(version) FROM cloud_sync_schema_migrations").fetchone()
            applied = int(row[0] or 0) if row else 0
            if applied > EVENT_SCHEMA_VERSION:
                raise RuntimeError(
                    f"Cloud event schema version {applied} is newer than supported "
                    f"version {EVENT_SCHEMA_VERSION}."
                )
            for statement in EVENT_SCHEMA_STATEMENTS[1:]:
                connection.execute(statement)
            connection.execute(
                "INSERT INTO cloud_sync_schema_migrations(version) VALUES (%s) ON CONFLICT(version) DO NOTHING",
                (EVENT_SCHEMA_VERSION,),
            )

    def ping(self) -> None:
        """Verify the snapshot database connection without mutating state."""

        with self._connect() as connection:
            connection.execute("SELECT 1")

    def push_events(
        self,
        user_id: str,
        *,
        session_id: str,
        parent_revision: int,
        device_id: str,
        event_id: str,
        envelope: dict[str, object],
        checksum: str,
        event_ids: list[str] | None = None,
    ) -> dict[str, object]:
        """Append one encrypted event batch with optimistic revision control."""

        if not user_id or not session_id or not device_id or not event_id:
            raise ValueError("Sync event identity is required.")
        with self._connect() as connection:
            connection.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"sync:{user_id}:{session_id}",))
            batch_event_ids = [str(value) for value in (event_ids or []) if value]
            existing = connection.execute(
                "SELECT revision,checksum FROM cloud_sync_events WHERE user_id=%s AND session_id=%s AND event_id=%s",
                (user_id, session_id, event_id),
            ).fetchone()
            if existing is not None:
                revision = int(existing[0])
                if str(existing[1]) != checksum:
                    raise ValueError("Event batch id conflicts with an existing ciphertext.")
                return {"event_id": event_id, "accepted_event_ids": batch_event_ids, "revision": revision, "head_revision": revision, "idempotent": True}
            if batch_event_ids:
                prior_rows = connection.execute(
                    "SELECT event_ids_json FROM cloud_sync_events WHERE user_id=%s AND session_id=%s",
                    (user_id, session_id),
                ).fetchall()
                prior_ids = {
                    str(value)
                    for row in prior_rows
                    for value in (row[0] if isinstance(row[0], list) else [])
                }
                overlap = prior_ids.intersection(batch_event_ids)
                if overlap:
                    raise ValueError("Event id was already accepted in another batch.")
            head = connection.execute(
                "SELECT head_revision FROM cloud_sync_sessions WHERE user_id=%s AND session_id=%s FOR UPDATE",
                (user_id, session_id),
            ).fetchone()
            current = int(head[0]) if head is not None else 0
            if parent_revision != current:
                raise CloudSyncConflict("Cloud sync head changed; pull before pushing again.")
            revision = current + 1
            connection.execute(
                """INSERT INTO cloud_sync_sessions(user_id,session_id,head_revision,baseline_event_id)
                VALUES (%s,%s,%s,%s)
                ON CONFLICT(user_id,session_id) DO UPDATE SET head_revision=excluded.head_revision,updated_at=CURRENT_TIMESTAMP""",
                (user_id, session_id, revision, event_id if parent_revision == 0 else None),
            )
            connection.execute(
                """INSERT INTO cloud_sync_events
                (user_id,session_id,revision,event_id,parent_revision,device_id,envelope_json,checksum,event_ids_json)
                VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s::jsonb)""",
                (user_id, session_id, revision, event_id, parent_revision, device_id, json.dumps(envelope), checksum, json.dumps(batch_event_ids)),
            )
        return {"event_id": event_id, "accepted_event_ids": batch_event_ids, "revision": revision, "head_revision": revision, "idempotent": False}

    def pull_events(self, user_id: str, *, session_id: str, after_revision: int) -> dict[str, object]:
        """Return encrypted event batches after ``after_revision``."""

        with self._connect() as connection:
            rows = connection.execute(
                """SELECT revision,event_id,parent_revision,device_id,envelope_json,checksum,event_ids_json,created_at
                FROM cloud_sync_events WHERE user_id=%s AND session_id=%s AND revision>%s
                ORDER BY revision""",
                (user_id, session_id, after_revision),
            ).fetchall()
            head = connection.execute(
                "SELECT head_revision FROM cloud_sync_sessions WHERE user_id=%s AND session_id=%s",
                (user_id, session_id),
            ).fetchone()
        return {
            "session_id": session_id,
            "head_revision": int(head[0]) if head is not None else after_revision,
            "events": [
                {
                    "revision": int(row[0]),
                    "event_id": str(row[1]),
                    "parent_revision": int(row[2]),
                    "device_id": str(row[3]),
                    "envelope": dict(row[4]) if isinstance(row[4], dict) else row[4],
                    "checksum": str(row[5]),
                    "event_ids": list(row[6]) if isinstance(row[6], list) else [],
                    "created_at": row[7].isoformat() if hasattr(row[7], "isoformat") else str(row[7]),
                }
                for row in rows
            ],
        }

    def ensure_user_key(self, user_id: str, dek: bytes) -> None:
        if len(dek) != 32:
            raise ValueError("The user data key must contain exactly 32 bytes.")
        with self._connect() as connection:
            row = connection.execute(
                "SELECT wrapped_dek,nonce,master_key_version FROM cloud_user_keys WHERE user_id=%s", (user_id,)
            ).fetchone()
            if row is not None:
                existing = self.master_cipher.unwrap(user_id, str(row[2]), bytes(row[1]), bytes(row[0]))
                if existing != dek:
                    raise CloudSyncConflict("The cloud user data key does not match the existing account key.")
                return
            version, nonce, wrapped = self.master_cipher.wrap(user_id, dek)
            connection.execute(
                """INSERT INTO cloud_user_keys(user_id,wrapped_dek,nonce,master_key_version)
                VALUES (%s,%s,%s,%s) ON CONFLICT(user_id) DO NOTHING""",
                (user_id, wrapped, nonce, version),
            )
            # A concurrent first login may have won the insert race. Always
            # read the committed envelope back and compare it with the local
            # key instead of silently accepting a different DEK.
            stored = connection.execute(
                "SELECT wrapped_dek,nonce,master_key_version FROM cloud_user_keys WHERE user_id=%s", (user_id,)
            ).fetchone()
            if stored is None:
                raise RuntimeError("Cloud user data key was not persisted.")
            existing = self.master_cipher.unwrap(user_id, str(stored[2]), bytes(stored[1]), bytes(stored[0]))
            if existing != dek:
                raise CloudSyncConflict("The cloud user data key does not match the existing account key.")

    def recover_user_key(self, user_id: str) -> bytes | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT wrapped_dek,nonce,master_key_version FROM cloud_user_keys WHERE user_id=%s",
                (user_id,),
            ).fetchone()
        if row is None:
            return None
        return self.master_cipher.unwrap(user_id, str(row[2]), bytes(row[1]), bytes(row[0]))
