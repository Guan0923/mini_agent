"""Public authentication errors and identity types."""

from __future__ import annotations

from backend.storage.auth.types import UserIdentity


class AuthError(ValueError):
    """A user-facing authentication failure with a safe message."""


class RateLimitError(AuthError):
    """The request exceeded a deliberately conservative auth limit."""

    def __init__(self, message: str = "请求过于频繁，请稍后再试。", retry_after: int = 60) -> None:
        super().__init__(message)
        self.retry_after = max(1, retry_after)


__all__ = ["AuthError", "RateLimitError", "UserIdentity"]
