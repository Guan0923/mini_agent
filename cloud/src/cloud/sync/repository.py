"""PostgreSQL heads and encrypted JSON event batches."""

from __future__ import annotations

import json

from cloud.storage.crypto import CloudMasterCipher
from cloud.storage.event_schema import EVENT_SCHEMA_STATEMENTS, EVENT_SCHEMA_VERSION


class CloudSyncConflict(RuntimeError):
    """The client is not based on the current cloud head."""


class PostgresCloudEventRepository:
    """Persist ciphertext envelopes; the cloud never parses business JSON."""

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
            row = connection.execute("SELECT MAX(version) FROM cloud_event_schema_migrations").fetchone()
            applied = int(row[0] or 0) if row else 0
            if applied > EVENT_SCHEMA_VERSION:
                raise RuntimeError(
                    f"Cloud event schema version {applied} is newer than supported version {EVENT_SCHEMA_VERSION}."
                )
            for statement in EVENT_SCHEMA_STATEMENTS[1:]:
                connection.execute(statement)
            connection.execute(
                "INSERT INTO cloud_event_schema_migrations(version) VALUES (%s) ON CONFLICT(version) DO NOTHING",
                (EVENT_SCHEMA_VERSION,),
            )

    def ping(self) -> None:
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
        if not user_id or not session_id or not device_id or not event_id:
            raise ValueError("Sync event identity is required.")
        ids = [str(value) for value in (event_ids or []) if value]
        if len(ids) != len(set(ids)):
            raise ValueError("Event IDs must be unique within a batch.")
        with self._connect() as connection:
            connection.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"sync:{user_id}:{session_id}",))
            existing = connection.execute(
                "SELECT revision,checksum FROM cloud_event_batches WHERE user_id=%s AND session_id=%s AND batch_id=%s",
                (user_id, session_id, event_id),
            ).fetchone()
            if existing is not None:
                if str(existing[1]) != checksum:
                    raise ValueError("Event batch id conflicts with an existing ciphertext.")
                accepted = connection.execute(
                    "SELECT event_id FROM cloud_event_ids WHERE user_id=%s AND session_id=%s AND batch_id=%s ORDER BY event_id",
                    (user_id, session_id, event_id),
                ).fetchall()
                revision = int(existing[0])
                return {
                    "event_id": event_id,
                    "accepted_event_ids": [str(row[0]) for row in accepted],
                    "revision": revision,
                    "head_revision": revision,
                    "idempotent": True,
                }
            prior = (
                connection.execute(
                    "SELECT event_id,revision,batch_id FROM cloud_event_ids WHERE user_id=%s AND session_id=%s AND event_id = ANY(%s)",
                    (user_id, session_id, ids),
                ).fetchall()
                if ids
                else []
            )
            if prior:
                prior_ids = {str(row[0]) for row in prior}
                if prior_ids != set(ids) or len({int(row[1]) for row in prior}) != 1:
                    raise ValueError("Event ID was already accepted in another batch.")
                revision = int(prior[0][1])
                return {
                    "event_id": event_id,
                    "accepted_event_ids": ids,
                    "revision": revision,
                    "head_revision": revision,
                    "idempotent": True,
                }
            head = connection.execute(
                "SELECT head_revision FROM cloud_event_heads WHERE user_id=%s AND session_id=%s FOR UPDATE",
                (user_id, session_id),
            ).fetchone()
            current = int(head[0]) if head is not None else 0
            if parent_revision != current:
                raise CloudSyncConflict("Cloud sync head changed; pull before pushing again.")
            revision = current + 1
            connection.execute(
                """INSERT INTO cloud_event_heads(user_id,session_id,head_revision,baseline_batch_id)
                VALUES (%s,%s,%s,%s)
                ON CONFLICT(user_id,session_id) DO UPDATE SET head_revision=excluded.head_revision,updated_at=CURRENT_TIMESTAMP""",
                (user_id, session_id, revision, event_id if parent_revision == 0 else None),
            )
            nonce = str(envelope.get("nonce") or "")
            connection.execute(
                """INSERT INTO cloud_event_batches
                (user_id,session_id,revision,batch_id,parent_revision,device_id,nonce,envelope_json,checksum)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s)""",
                (
                    user_id,
                    session_id,
                    revision,
                    event_id,
                    parent_revision,
                    device_id,
                    nonce,
                    json.dumps(envelope),
                    checksum,
                ),
            )
            for value in ids:
                connection.execute(
                    "INSERT INTO cloud_event_ids(user_id,session_id,event_id,revision,batch_id) VALUES (%s,%s,%s,%s,%s)",
                    (user_id, session_id, value, revision, event_id),
                )
        return {
            "event_id": event_id,
            "accepted_event_ids": ids,
            "revision": revision,
            "head_revision": revision,
            "idempotent": False,
        }

    def pull_events(self, user_id: str, *, session_id: str, after_revision: int) -> dict[str, object]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT revision,batch_id,parent_revision,device_id,nonce,envelope_json,checksum,created_at
                FROM cloud_event_batches WHERE user_id=%s AND session_id=%s AND revision>%s ORDER BY revision""",
                (user_id, session_id, after_revision),
            ).fetchall()
            head = connection.execute(
                "SELECT head_revision FROM cloud_event_heads WHERE user_id=%s AND session_id=%s",
                (user_id, session_id),
            ).fetchone()
            ids = {str(row[0]): [] for row in rows}
            for row in connection.execute(
                "SELECT batch_id,event_id FROM cloud_event_ids WHERE user_id=%s AND session_id=%s AND revision>%s ORDER BY revision,event_id",
                (user_id, session_id, after_revision),
            ).fetchall():
                ids.setdefault(str(row[0]), []).append(str(row[1]))
        return {
            "session_id": session_id,
            "head_revision": int(head[0]) if head is not None else after_revision,
            "events": [
                {
                    "revision": int(row[0]),
                    "event_id": str(row[1]),
                    "parent_revision": int(row[2]),
                    "device_id": str(row[3]),
                    "envelope": dict(row[5]) if isinstance(row[5], dict) else row[5],
                    "checksum": str(row[6]),
                    "event_ids": ids.get(str(row[1]), []),
                    "created_at": row[7].isoformat() if hasattr(row[7], "isoformat") else str(row[7]),
                }
                for row in rows
            ],
        }

    def list_heads(self, user_id: str) -> list[dict[str, object]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT session_id,head_revision,updated_at FROM cloud_event_heads WHERE user_id=%s ORDER BY session_id",
                (user_id,),
            ).fetchall()
        return [
            {
                "session_id": str(row[0]),
                "head_revision": int(row[1]),
                "updated_at": row[2].isoformat() if hasattr(row[2], "isoformat") else str(row[2]),
            }
            for row in rows
        ]

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
                "INSERT INTO cloud_user_keys(user_id,wrapped_dek,nonce,master_key_version) VALUES (%s,%s,%s,%s) ON CONFLICT(user_id) DO NOTHING",
                (user_id, wrapped, nonce, version),
            )
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
                "SELECT wrapped_dek,nonce,master_key_version FROM cloud_user_keys WHERE user_id=%s", (user_id,)
            ).fetchone()
        return None if row is None else self.master_cipher.unwrap(user_id, str(row[2]), bytes(row[1]), bytes(row[0]))
