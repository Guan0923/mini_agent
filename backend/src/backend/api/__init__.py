"""FastAPI web application for the mini-agent (chat + benchmark).

The public API was reorganized into focused subpackages. These aliases keep
older integrations importing the former flat modules working while the
implementation lives in ``api.auth``, ``api.chat``, ``api.sessions`` and
``api.shared``.
"""

from __future__ import annotations

import importlib
import sys

_COMPAT_MODULES = {
    "auth_dependencies": ".auth.dependencies",
    "auth_mail": ".auth.mail",
    "auth_router": ".auth.routes",
    "auth_schema": "backend.storage.auth.schema",
    "auth_service": ".auth.service",
    "auth_store": ".auth_store",
    "auth_types": ".auth.types",
    "benchmark_app": ".shared.benchmark",
    "decisions": ".chat.decisions",
    "info": ".shared.info",
    "interrupts": ".chat.interrupts",
    "settings_store": "backend.storage.postgres.settings",
}


def _register_compat_modules() -> None:
    for legacy_name, target in _COMPAT_MODULES.items():
        module_name = f"{__name__}.{legacy_name}"
        if module_name in sys.modules:
            continue
        module = (
            importlib.import_module(target, __name__) if target.startswith(".") else importlib.import_module(target)
        )
        sys.modules[module_name] = module


_register_compat_modules()

__all__ = []
