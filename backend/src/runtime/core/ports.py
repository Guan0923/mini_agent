"""Runtime persistence ports shared by orchestration and adapters."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from backend.domain import RuntimeMessage

if TYPE_CHECKING:
    from .context import RuntimeState


class RuntimeStore(Protocol):
    """Persist resumable runtime state and ordered runtime messages."""

    def save_runtime(self, state: RuntimeState) -> None: ...

    def append_runtime_message(self, session_id: str, run_id: str, message: RuntimeMessage) -> None: ...
