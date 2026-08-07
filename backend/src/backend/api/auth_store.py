"""Compatibility import for the historical Web authentication repository."""

from backend.storage.auth.crypto import _decrypt_secret, _encrypt_secret, _key_material
from backend.storage.auth.repository import AuthStore
from backend.storage.auth.schema import SCHEMA
from backend.storage.auth.types import UserIdentity
from backend.storage.settings_contract import (
    DEFAULT_AGENT_CONFIG,
    DEFAULT_CAPABILITY_CONFIG,
    DEFAULT_PROFILE,
    DEFAULT_PROVIDER_CONFIG,
    SUPPORTED_DISPLAY_MODES,
)

__all__ = [
    "AuthStore",
    "DEFAULT_AGENT_CONFIG",
    "DEFAULT_CAPABILITY_CONFIG",
    "DEFAULT_PROFILE",
    "DEFAULT_PROVIDER_CONFIG",
    "SUPPORTED_DISPLAY_MODES",
    "SCHEMA",
    "UserIdentity",
    "_decrypt_secret",
    "_encrypt_secret",
    "_key_material",
]
