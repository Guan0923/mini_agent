"""Runtime node, checkpoint, and message persistence mixins."""

from .checkpoints import SQLiteCheckpointMixin
from .nodes import SQLiteNodeMixin
from .records import SQLiteJsonObjectMixin


class SQLiteRuntimeMixin(SQLiteNodeMixin, SQLiteCheckpointMixin, SQLiteJsonObjectMixin):
    """Compose runtime persistence responsibilities for SQLiteStore."""


__all__ = ["SQLiteRuntimeMixin"]
