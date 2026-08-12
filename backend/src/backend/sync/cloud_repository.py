"""Provider-neutral values for the local snapshot repository port.

The backend never connects to PostgreSQL.  The concrete HTTP adapter lives
under :mod:`backend.cloud`, while the cloud service owns its PostgreSQL
repository.  These small values remain here because the snapshot manager and
tests need a shared, deployment-neutral contract.
"""

from __future__ import annotations

from dataclasses import dataclass


class CloudSyncConflict(RuntimeError):
    """The local client is not based on the current cloud head."""


@dataclass(frozen=True)
class EncryptedSnapshotChunk:
    """One authenticated ciphertext chunk produced by the local client."""

    sequence: int
    nonce: bytes
    ciphertext: bytes
    checksum: str


__all__ = ["CloudSyncConflict", "EncryptedSnapshotChunk"]
