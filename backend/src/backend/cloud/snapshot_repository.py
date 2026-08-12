"""Local repository port backed by the remote cloud API."""

from __future__ import annotations

from collections.abc import Callable

from backend.sync.cloud_repository import EncryptedSnapshotChunk

from .client import CloudClient


class HttpCloudSnapshotRepository:
    """Match the local SnapshotManager repository port without PostgreSQL."""

    def __init__(
        self,
        base_url: str,
        token_for_user: Callable[[str], str],
        clear_token_for_user: Callable[[str], None] | None = None,
    ) -> None:
        self.base_url = base_url
        self.token_for_user = token_for_user
        self.clear_token_for_user = clear_token_for_user

    def _client(self, user_id: str) -> CloudClient:
        return CloudClient(
            self.base_url,
            token=self.token_for_user(user_id),
            on_auth_expired=(lambda: self.clear_token_for_user(user_id)) if self.clear_token_for_user else None,
        )

    def _call(self, user_id: str, method: str, *args, **kwargs):
        """Run one request and release the per-call HTTP session.

        Snapshot jobs can perform many chunk requests and may run for a long
        time.  Keeping a short-lived ``requests.Session`` alive for every
        adapter call would retain sockets until garbage collection, so the
        adapter owns the lifecycle explicitly.
        """

        client = self._client(user_id)
        try:
            return getattr(client, method)(*args, **kwargs)
        finally:
            client.close()

    def ensure_user_key(self, user_id: str, dek: bytes) -> None:
        self._call(user_id, "ensure_user_key", dek)

    def recover_user_key(self, user_id: str) -> bytes | None:
        return self._call(user_id, "recover_user_key")

    def head(self, user_id: str) -> dict[str, object] | None:
        snapshots = self._call(user_id, "list_snapshots")
        return snapshots[0] if snapshots else None

    def list_snapshots(self, user_id: str) -> list[dict[str, object]]:
        return self._call(user_id, "list_snapshots")

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
        return self._call(
            user_id,
            "begin_snapshot",
            snapshot_id=snapshot_id,
            parent_snapshot_id=parent_snapshot_id,
            local_revision=local_revision,
            device_id=device_id,
            force=force,
        )

    def append_chunk(self, snapshot_id: str, chunk: EncryptedSnapshotChunk, *, user_id: str | None = None) -> None:
        """Append a chunk when the caller supplies the owning identity.

        ``SnapshotManager`` uses ``append_chunk_for_user`` so the identity is
        never inferred from a snapshot id.  The optional keyword keeps the
        historical repository port usable for explicit callers while still
        refusing an unauthenticated upload.
        """
        if not user_id:
            raise ValueError("user_id is required for cloud snapshot uploads.")
        self.append_chunk_for_user(user_id, snapshot_id, chunk)

    def append_chunk_for_user(self, user_id: str, snapshot_id: str, chunk: EncryptedSnapshotChunk) -> None:
        self._call(user_id, "append_chunk", snapshot_id, chunk)

    def complete_snapshot(
        self,
        user_id: str,
        snapshot_id: str,
        *,
        archive_sha256: str,
        archive_size: int,
        chunk_count: int,
    ) -> None:
        self._call(
            user_id,
            "complete_snapshot",
            snapshot_id,
            archive_sha256=archive_sha256,
            archive_size=archive_size,
            chunk_count=chunk_count,
        )

    def fail_snapshot(self, user_id: str, snapshot_id: str) -> None:
        self._call(user_id, "fail_snapshot", snapshot_id)

    def download(self, user_id: str, snapshot_id: str) -> tuple[dict[str, object], list[EncryptedSnapshotChunk]]:
        return self._call(user_id, "download", snapshot_id)


__all__ = ["HttpCloudSnapshotRepository"]
