"""Provider-neutral cloud authentication types."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class UserIdentity:
    id: str
    email: str | None = None
    kind: str = "account"

    @property
    def is_guest(self) -> bool:
        return self.kind == "guest"


class AuthStorageUnavailable(RuntimeError):
    """The cloud authentication database is unavailable."""


class AuthError(ValueError):
    """A user-facing authentication error."""


class RateLimitError(AuthError):
    """A rate limit was exceeded."""

    def __init__(self, message: str = "请求过于频繁，请稍后再试。", retry_after: int = 60) -> None:
        super().__init__(message)
        self.retry_after = retry_after
