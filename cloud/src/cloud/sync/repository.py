"""PostgreSQL metadata and encrypted chunk storage for user cloud snapshots."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime

from cloud.storage.crypto import CloudMasterCipher
from cloud.storage.snapshot_schema import SNAPSHOT_SCHEMA_STATEMENTS, SNAPSHOT_SCHEMA_VERSION


class CloudSyncConflict(RuntimeError):
    """The local client is not based on the current cloud head."""


@dataclass(frozen=True)
class EncryptedSnapshotChunk:
    sequence: int
    nonce: bytes
    ciphertext: bytes
    checksum: str


class PostgresCloudSnapshotRepository:
    """Keep encrypted snapshots in PostgreSQL; plaintext never crosses this port."""

    def __init__(self, database_url: str, *, master_cipher: CloudMasterCipher | None = None) -> None:
        self.database_url = database_url
        self.master_cipher = master_cipher or CloudMasterCipher()
        self.initialize()

    def _connect(self):
        import psycopg

        return psycopg.connect(self.database_url, connect_timeout=10)

    def initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(SNAPSHOT_SCHEMA_STATEMENTS[0])
            row = connection.execute("SELECT MAX(version) FROM cloud_sync_schema_migrations").fetchone()
            applied = int(row[0] or 0) if row else 0
            if applied > SNAPSHOT_SCHEMA_VERSION:
                raise RuntimeError(
                    f"Cloud snapshot schema version {applied} is newer than supported "
                    f"version {SNAPSHOT_SCHEMA_VERSION}."
                )
            for statement in SNAPSHOT_SCHEMA_STATEMENTS[1:]:
                connection.execute(statement)
            connection.execute(
                "INSERT INTO cloud_sync_schema_migrations(version) VALUES (%s) ON CONFLICT(version) DO NOTHING",
                (SNAPSHOT_SCHEMA_VERSION,),
            )

    def ping(self) -> None:
        """Verify the snapshot database connection without mutating state."""

        with self._connect() as connection:
            connection.execute("SELECT 1")

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

    def head(self, user_id: str) -> dict[str, object] | None:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT id,version,local_revision,device_id,archive_size,chunk_count,completed_at
                FROM cloud_snapshots WHERE user_id=%s AND status='complete'
                ORDER BY version DESC LIMIT 1""",
                (user_id,),
            ).fetchone()
        return self._snapshot(row) if row is not None else None

    def list_snapshots(self, user_id: str) -> list[dict[str, object]]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT id,version,local_revision,device_id,archive_size,chunk_count,completed_at
                FROM cloud_snapshots WHERE user_id=%s AND status='complete'
                ORDER BY version DESC LIMIT 3""",
                (user_id,),
            ).fetchall()
        return [self._snapshot(row) for row in rows]

    @staticmethod
    def _snapshot(row) -> dict[str, object]:
        completed = row[6]
        return {
            "id": str(row[0]),
            "version": int(row[1]),
            "local_revision": int(row[2]),
            "device_id": str(row[3]),
            "archive_size": int(row[4]),
            "chunk_count": int(row[5]),
            "completed_at": completed.isoformat() if isinstance(completed, datetime) else str(completed or ""),
        }

    def begin_snapshot(
        self,
        *,
        snapshot_id: str,
        user_id: str,
        parent_snapshot_id: str | None,
        local_revision: int,
        device_id: str,
        force: bool,
    ) -> int:
        with self._connect() as connection:
            connection.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"cloud:{user_id}",))
            existing_snapshot = connection.execute(
                "SELECT user_id,status FROM cloud_snapshots WHERE id=%s", (snapshot_id,)
            ).fetchone()
            if existing_snapshot is not None:
                if str(existing_snapshot[0]) != user_id:
                    raise ValueError("Snapshot id is already owned by another user.")
                raise ValueError("Snapshot id is already in use.")
            row = connection.execute(
                """SELECT id,version FROM cloud_snapshots
                WHERE user_id=%s AND status='complete' ORDER BY version DESC LIMIT 1 FOR UPDATE""",
                (user_id,),
            ).fetchone()
            head_id = str(row[0]) if row is not None else None
            if not force and head_id != parent_snapshot_id:
                raise CloudSyncConflict("Cloud head changed since the last local synchronization.")
            # Failed or abandoned uploads retain their audit row, so deriving
            # the next version from the complete head alone can collide with a
            # previous attempt's UNIQUE(user_id, version) slot.  Reserve a
            # monotonically increasing version across every status instead.
            max_version = connection.execute(
                "SELECT COALESCE(MAX(version), 0) FROM cloud_snapshots WHERE user_id=%s",
                (user_id,),
            ).fetchone()
            version = int(max_version[0] if max_version is not None else 0) + 1
            connection.execute(
                """INSERT INTO cloud_snapshots
                (id,user_id,version,parent_snapshot_id,status,local_revision,device_id)
                VALUES (%s,%s,%s,%s,'uploading',%s,%s)""",
                (snapshot_id, user_id, version, head_id, local_revision, device_id),
            )
        return version

    def append_chunk(self, user_id: str, snapshot_id: str, chunk: EncryptedSnapshotChunk) -> None:
        if chunk.sequence < 0 or len(chunk.nonce) != 12 or len(chunk.ciphertext) < 16:
            raise ValueError("Snapshot chunk format is invalid.")
        if (
            not re.fullmatch(r"[0-9a-fA-F]{64}", chunk.checksum)
            or hashlib.sha256(chunk.ciphertext).hexdigest() != chunk.checksum
        ):
            raise ValueError("Snapshot chunk checksum is invalid.")
        with self._connect() as connection:
            snapshot = connection.execute(
                "SELECT 1 FROM cloud_snapshots WHERE id=%s AND user_id=%s AND status='uploading'",
                (snapshot_id, user_id),
            ).fetchone()
            if snapshot is None:
                raise ValueError("Snapshot is not in an uploadable state or does not belong to this user.")
            existing = connection.execute(
                "SELECT nonce,ciphertext,checksum FROM cloud_snapshot_chunks WHERE snapshot_id=%s AND sequence=%s",
                (snapshot_id, chunk.sequence),
            ).fetchone()
            if existing is not None:
                if (
                    bytes(existing[0]) == chunk.nonce
                    and bytes(existing[1]) == chunk.ciphertext
                    and str(existing[2]) == chunk.checksum
                ):
                    return
                raise ValueError("Snapshot chunk conflicts with an existing upload.")
            connection.execute(
                """INSERT INTO cloud_snapshot_chunks(snapshot_id,sequence,nonce,ciphertext,checksum)
                VALUES (%s,%s,%s,%s,%s)""",
                (snapshot_id, chunk.sequence, chunk.nonce, chunk.ciphertext, chunk.checksum),
            )

    def complete_snapshot(
        self,
        user_id: str,
        snapshot_id: str,
        *,
        archive_sha256: str,
        archive_size: int,
        chunk_count: int,
    ) -> None:
        if archive_size < 1 or chunk_count < 1:
            raise ValueError("Snapshot metadata is invalid.")
        if not re.fullmatch(r"[0-9a-fA-F]{64}", archive_sha256):
            raise ValueError("Snapshot archive checksum is invalid.")
        with self._connect() as connection:
            row = connection.execute(
                """SELECT status,archive_sha256,archive_size,chunk_count
                FROM cloud_snapshots WHERE id=%s AND user_id=%s""",
                (snapshot_id, user_id),
            ).fetchone()
            if row is None:
                raise ValueError("Snapshot is not in an uploadable state.")
            if str(row[0]) == "complete":
                if str(row[1]) == archive_sha256 and int(row[2]) == archive_size and int(row[3]) == chunk_count:
                    return
                raise ValueError("Snapshot completion conflicts with existing metadata.")
            if str(row[0]) != "uploading":
                raise ValueError("Snapshot is not in an uploadable state.")
            count_row = connection.execute(
                "SELECT COUNT(*), COALESCE(MAX(sequence), -1) FROM cloud_snapshot_chunks WHERE snapshot_id=%s",
                (snapshot_id,),
            ).fetchone()
            stored_count = int(count_row[0] or 0) if count_row else 0
            max_sequence = int(count_row[1] if count_row else -1)
            if stored_count != chunk_count or max_sequence != chunk_count - 1:
                raise ValueError("Snapshot chunk count does not match the uploaded chunks.")
            connection.execute(
                """UPDATE cloud_snapshots SET status='complete',archive_sha256=%s,archive_size=%s,
                chunk_count=%s,completed_at=CURRENT_TIMESTAMP WHERE id=%s AND user_id=%s AND status='uploading'""",
                (archive_sha256, archive_size, chunk_count, snapshot_id, user_id),
            )
            stale = connection.execute(
                """SELECT id FROM cloud_snapshots WHERE user_id=%s AND status='complete'
                ORDER BY version DESC OFFSET 3""",
                (user_id,),
            ).fetchall()
            if stale:
                connection.execute(
                    "DELETE FROM cloud_snapshots WHERE id = ANY(%s)",
                    ([str(row[0]) for row in stale],),
                )

    def fail_snapshot(self, user_id: str, snapshot_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM cloud_snapshot_chunks WHERE snapshot_id=%s AND EXISTS "
                "(SELECT 1 FROM cloud_snapshots WHERE id=%s AND user_id=%s AND status='uploading')",
                (snapshot_id, snapshot_id, user_id),
            )
            connection.execute(
                "UPDATE cloud_snapshots SET status='failed' WHERE id=%s AND user_id=%s AND status='uploading'",
                (snapshot_id, user_id),
            )

    def download(self, user_id: str, snapshot_id: str) -> tuple[dict[str, object], list[EncryptedSnapshotChunk]]:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT id,version,local_revision,device_id,archive_size,chunk_count,completed_at,archive_sha256
                FROM cloud_snapshots WHERE id=%s AND user_id=%s AND status='complete'""",
                (snapshot_id, user_id),
            ).fetchone()
            if row is None:
                raise ValueError("Cloud snapshot not found.")
            chunk_rows = connection.execute(
                """SELECT sequence,nonce,ciphertext,checksum FROM cloud_snapshot_chunks
                WHERE snapshot_id=%s ORDER BY sequence""",
                (snapshot_id,),
            ).fetchall()
        metadata = self._snapshot(row)
        metadata["archive_sha256"] = str(row[7])
        chunks = [
            EncryptedSnapshotChunk(int(item[0]), bytes(item[1]), bytes(item[2]), str(item[3])) for item in chunk_rows
        ]
        return metadata, chunks
