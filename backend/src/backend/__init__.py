"""Mini-Agent: a terminal-first agent execution lab."""

from __future__ import annotations

from importlib import import_module
from typing import Any

__version__ = "0.3.0"

__all__ = ["AgentRunner", "__version__"]


def __getattr__(name: str) -> Any:
    """Load convenience exports without importing the application graph eagerly."""

    if name != "AgentRunner":
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(".runtime", __name__), name)
    globals()[name] = value
    return value
