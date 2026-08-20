"""HTTP adapter for encrypted JSON event synchronization."""

from __future__ import annotations

from collections.abc import Callable, Mapping

from .client import CloudClient


class HttpCloudEventRepository:
    """Bind a per-user cloud token to the event push/pull API."""

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

    def ensure_user_key(self, user_id: str, key: bytes) -> None:
        self._client(user_id).ensure_user_key(key)

    def recover_user_key(self, user_id: str) -> bytes | None:
        return self._client(user_id).recover_user_key()

    def push_events(
        self,
        user_id: str,
        *,
        session_id: str,
        parent_revision: int,
        device_id: str,
        event_id: str,
        envelope: Mapping[str, object],
        checksum: str,
        event_ids: list[str] | None = None,
    ) -> dict[str, object]:
        return self._client(user_id).push_events(
            session_id=session_id,
            parent_revision=parent_revision,
            device_id=device_id,
            event_id=event_id,
            event_ids=event_ids,
            envelope=envelope,
            checksum=checksum,
        )

    def pull_events(self, user_id: str, *, session_id: str, after_revision: int) -> dict[str, object]:
        return self._client(user_id).pull_events(session_id=session_id, after_revision=after_revision)

    def list_heads(self, user_id: str) -> list[dict[str, object]]:
        return self._client(user_id).list_sync_heads()


__all__ = ["HttpCloudEventRepository"]
