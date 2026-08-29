"""Minimal import bootstrap for the isolated pywin32 service host."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def _install_backend_package() -> None:
    if "backend" in sys.modules:
        return
    source_root = Path(__file__).resolve().parent
    spec = importlib.util.spec_from_file_location(
        "backend",
        source_root / "__init__.py",
        submodule_search_locations=[str(source_root)],
    )
    if spec is None or spec.loader is None:
        raise ImportError("Broker backend package bootstrap failed")
    module = importlib.util.module_from_spec(spec)
    sys.modules["backend"] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop("backend", None)
        raise


_install_backend_package()

from backend.sandbox.service_main import MiniAgentSandboxBrokerService  # noqa: E402

__all__ = ["MiniAgentSandboxBrokerService"]
