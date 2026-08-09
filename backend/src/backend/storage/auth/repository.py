"""Composed authentication repository backed by a local SQLite database."""

from __future__ import annotations

from .database import AuthDatabaseMixin
from .identities import AuthIdentityMixin
from .settings import AuthSettingsMixin
from .tokens import AuthTokenMixin


class AuthStore(AuthDatabaseMixin, AuthSettingsMixin, AuthIdentityMixin, AuthTokenMixin):
    """Own authentication, settings, and token mutations behind one facade."""

    def ping(self) -> None:
        with self._connection() as connection:
            connection.execute("SELECT 1")

    def close(self) -> None:
        """SQLite connections are opened per operation and need no shared cleanup."""


__all__ = ["AuthStore"]
