"""Local authentication boundary for the loopback backend.

Account authority lives in :mod:`cloud`.  The backend package intentionally
exports only the provider-neutral identity contract and the local session
store; it does not import the historical account/password repository.
"""

from .local import LocalAuthStore
from .types import AuthRepository, AuthStorageUnavailable, UserIdentity

__all__ = ["AuthRepository", "AuthStorageUnavailable", "LocalAuthStore", "UserIdentity"]
