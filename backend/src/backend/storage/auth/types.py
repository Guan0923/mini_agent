"""Storage-neutral identity value used by authentication adapters."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class UserIdentity:
    id: str
    email: str
    legacy_owner: bool = False
