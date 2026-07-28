"""Composed transcript rendering behavior for TerminalView."""

from ..rendering.events import TranscriptEventMixin
from ..rendering.retention import TranscriptRetentionMixin
from ..rendering.state import TranscriptStateMixin


class TranscriptRenderingMixin(
    TranscriptStateMixin,
    TranscriptEventMixin,
    TranscriptRetentionMixin,
):
    """Own transcript buffering, structured nodes, and runtime-event rendering."""
