"""Application services and dependency composition."""

from __future__ import annotations

from importlib import import_module
from typing import Any

_EXPORTS = {
    "AgentApplication": ("services", "AgentApplication"),
    "build_application": ("factory", "build_application"),
    "build_runner": ("factory", "build_runner"),
    "build_session_store": ("factory", "build_session_store"),
}

__all__ = list(_EXPORTS)


def __getattr__(name: str) -> Any:
    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attribute = target
    value = getattr(import_module(f".{module_name}", __name__), attribute)
    globals()[name] = value
    return value
