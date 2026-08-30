"""Broker wire constants and canonical serialization helpers."""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

BROKER_VERSION = "3"
MAX_REQUEST_TTL_SECONDS = 60
MAX_CLOCK_SKEW_SECONDS = 5


def _canonical(value: Mapping[str, Any]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def _default_program_data() -> Path:
    root = os.environ.get("PROGRAMDATA") if os.name == "nt" else None
    return Path(root or (Path(tempfile.gettempdir()) / "mini-agent-programdata")) / "Mini-Agent" / "SandboxBroker"


def _atomic_temporary(parent: Path, prefix: str) -> tuple[int, str]:
    if os.name != "nt":
        return tempfile.mkstemp(prefix=prefix, dir=parent)
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0)
    for _ in range(16):
        temporary = parent / f"{prefix}{uuid.uuid4().hex}"
        try:
            return os.open(temporary, flags, 0o666), str(temporary)
        except FileExistsError:
            continue
    raise OSError("Could not allocate a Broker temporary file")
