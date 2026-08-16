"""Local snapshot synchronization ports."""

from __future__ import annotations

from typing import Protocol

from .cloud_repository import EncryptedSnapshotChunk


class SnapshotRepository(Protocol):
    """Repository contract implemented by the local HTTP cloud adapter."""

    def ensure_user_key(self, user_id: str, dek: bytes) -> None: ...

    def recover_user_key(self, user_id: str) -> bytes | None: ...

    def list_snapshots(self, user_id: str) -> list[dict[str, object]]: ...

    def begin_snapshot(
        self,
        *,
        snapshot_id: str,
        user_id: str,
        parent_snapshot_id: str | None,
        local_revision: int,
        device_id: str,
        force: bool,
    ) -> int: ...

    def append_chunk(self, snapshot_id: str, chunk: EncryptedSnapshotChunk, *, user_id: str | None = None) -> None: ...

    def complete_snapshot(
        self,
        user_id: str,
        snapshot_id: str,
        *,
        archive_sha256: str,
        archive_size: int,
        chunk_count: int,
    ) -> None: ...

    def fail_snapshot(self, user_id: str, snapshot_id: str) -> None: ...

    def download(self, user_id: str, snapshot_id: str) -> tuple[dict[str, object], list[EncryptedSnapshotChunk]]: ...


__all__ = ["SnapshotRepository"]
