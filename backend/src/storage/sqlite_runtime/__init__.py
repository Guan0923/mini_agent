"""Runtime node, checkpoint, and message persistence mixins."""

from .checkpoints import SQLiteCheckpointMixin
from .events import SQLiteRuntimeEventMixin
from .nodes import SQLiteNodeMixin
from .records import SQLiteJsonObjectMixin
from .traces import SQLiteTurnTraceMixin


class SQLiteRuntimeMixin(
    SQLiteRuntimeEventMixin,
    SQLiteNodeMixin,
    SQLiteCheckpointMixin,
    SQLiteJsonObjectMixin,
    SQLiteTurnTraceMixin,
):
    """Compose runtime persistence responsibilities for SQLiteStore."""


__all__ = ["SQLiteRuntimeMixin"]
