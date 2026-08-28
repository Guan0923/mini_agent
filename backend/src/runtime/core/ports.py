"""Runtime persistence ports shared by orchestration and adapters."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from .context import RuntimeState


class RuntimeStore(Protocol):
    """Persist resumable runtime state."""

    def save_runtime(self, state: RuntimeState) -> None: ...
