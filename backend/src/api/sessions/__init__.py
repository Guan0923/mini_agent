"""Session management API routes.

The route module imports the complete runtime composition graph.  Keep those
exports lazy so consumers that only need the pure transcript projection do
not initialize providers, tools, or MCP clients as an import side effect.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

_EXPORTS = {
    "BranchRequest",
    "CreateSessionRequest",
    "RenameSessionRequest",
    "SessionMessageInput",
    "TimezoneBody",
    "_require_active",
    "_store",
    "build_application",
    "router",
}


def __getattr__(name: str) -> Any:
    if name not in _EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(".routes", __name__), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted({*globals(), *_EXPORTS})


__all__ = sorted(_EXPORTS)
