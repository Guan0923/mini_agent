"""Lazy pywin32 module loading for Broker-native primitives."""

from __future__ import annotations

import os
from typing import Any

from ..errors import SandboxInitializationError


def _require_windows() -> None:
    if os.name != "nt":
        raise SandboxInitializationError("native sandbox security is available only on Windows")


def _modules() -> dict[str, Any]:
    _require_windows()
    try:
        import ntsecuritycon  # type: ignore[import-not-found]
        import pywintypes  # type: ignore[import-not-found]
        import win32api  # type: ignore[import-not-found]
        import win32con  # type: ignore[import-not-found]
        import win32event  # type: ignore[import-not-found]
        import win32file  # type: ignore[import-not-found]
        import win32job  # type: ignore[import-not-found]
        import win32net  # type: ignore[import-not-found]
        import win32netcon  # type: ignore[import-not-found]
        import win32pipe  # type: ignore[import-not-found]
        import win32process  # type: ignore[import-not-found]
        import win32security  # type: ignore[import-not-found]
        import win32service  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - platform dependency
        raise SandboxInitializationError("pywin32 is required by the Windows Sandbox Broker") from exc
    return {
        "api": win32api,
        "con": win32con,
        "event": win32event,
        "file": win32file,
        "job": win32job,
        "net": win32net,
        "netcon": win32netcon,
        "pipe": win32pipe,
        "process": win32process,
        "security": win32security,
        "service": win32service,
        "ntsecuritycon": ntsecuritycon,
        "types": pywintypes,
    }
