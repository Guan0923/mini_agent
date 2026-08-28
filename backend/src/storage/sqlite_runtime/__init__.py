"""Runtime node, checkpoint, and message persistence mixins."""

from .checkpoints import SQLiteCheckpointMixin
from .nodes import SQLiteNodeMixin
from .records import SQLiteJsonObjectMixin
from .traces import SQLiteTurnTraceMixin


class SQLiteRuntimeMixin(SQLiteNodeMixin, SQLiteCheckpointMixin, SQLiteJsonObjectMixin, SQLiteTurnTraceMixin):
    """Compose runtime persistence responsibilities for SQLiteStore."""


__all__ = ["SQLiteRuntimeMixin"]
