"""Compatibility import for authentication public types."""

from .auth.types import AuthError, RateLimitError, UserIdentity

__all__ = ["AuthError", "RateLimitError", "UserIdentity"]
