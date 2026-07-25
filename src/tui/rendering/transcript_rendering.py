"""Composed transcript rendering behavior for TerminalView."""

from .events import TranscriptEventMixin
from .retention import TranscriptRetentionMixin
from .state import TranscriptStateMixin


class TranscriptRenderingMixin(
    TranscriptStateMixin,
    TranscriptEventMixin,
    TranscriptRetentionMixin,
):
    """Own transcript buffering, structured nodes, and runtime-event rendering."""
