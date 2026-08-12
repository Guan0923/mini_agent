"""Storage-neutral identity value used by authentication adapters."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class UserIdentity:
    id: str
    email: str | None = None
    kind: str = "account"

    @property
    def is_guest(self) -> bool:
        return self.kind == "guest"


class AuthStorageUnavailable(RuntimeError):
    """The server-side authentication database is temporarily unavailable."""


class AuthRepository(Protocol):
    """Local identity/session contract consumed by the Web backend.

    Account credentials, verification challenges, device grants and rate
    limits for account operations belong to the cloud service.  The loopback
    backend only needs cached identities, guest state and hashed local
    browser/device sessions.
    """

    def user_by_id(self, user_id: str) -> UserIdentity | None: ...

    def upsert_identity(self, identity: UserIdentity) -> UserIdentity: ...

    def create_guest(self) -> UserIdentity: ...

    def get_or_create_guest(self) -> tuple[UserIdentity, bool]: ...

    def create_guest_session(self, user_id: str, *, ttl_seconds: int = 2_592_000) -> str: ...

    def delete_guest(self, user_id: str) -> None: ...

    def pending_guest_import(self, user_id: str) -> dict[str, object] | None: ...

    def set_pending_guest_import(self, user_id: str, guest_id: str) -> None: ...

    def finish_guest_import(self, user_id: str, decision: str) -> None: ...

    def consume_limit(self, key: str, action: str, limit: int, window_seconds: int) -> bool: ...

    def create_session(self, user_id: str, kind: str, *, ttl_seconds: int = 2_592_000) -> str: ...

    def resolve_token(self, token: str) -> tuple[UserIdentity, str] | None: ...

    def revoke_token(self, token: str) -> None: ...

    def revoke_user_sessions(self, user_id: str) -> None: ...

    def ping(self) -> None: ...

    def close(self) -> None: ...
