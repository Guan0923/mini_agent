"""PostgreSQL encrypted snapshot repository owned by cloud."""

from cloud.sync.repository import (
    CloudSyncConflict,
    EncryptedSnapshotChunk,
)
from cloud.sync.repository import (
    PostgresCloudSnapshotRepository as _PostgresCloudSnapshotRepository,
)

from .crypto import CloudMasterCipher, SecretDecryptionError


class PostgresCloudSnapshotRepository(_PostgresCloudSnapshotRepository):
    """Compatibility-named repository with cloud-local imports."""


__all__ = [
    "CloudMasterCipher",
    "CloudSyncConflict",
    "EncryptedSnapshotChunk",
    "PostgresCloudSnapshotRepository",
    "SecretDecryptionError",
]
