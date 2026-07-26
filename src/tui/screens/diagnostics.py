"""Best-effort diagnostics for one interactive TUI invocation."""

from __future__ import annotations

import json
import sys
import traceback
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import Any, TextIO
from uuid import uuid4

DiagnosticSink = Callable[[str, dict[str, object] | None, BaseException | None], None]


class TuiDiagnosticLogger:
    """Write thread-safe, line-buffered diagnostics without affecting the TUI."""

    def __init__(
        self,
        log_dir: Path,
        *,
        warning_sink: Callable[[str], None] | None = None,
    ) -> None:
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        self.tui_id = f"tui_{uuid4().hex}"
        self.path = log_dir / f"tui_{timestamp}_{self.tui_id[4:12]}.jsonl"
        self._warning_sink = warning_sink or (lambda message: print(message, file=sys.stderr))
        self._lock = Lock()
        self._sequence = 0
        self._session_id: str | None = None
        self._run_id: str | None = None
        self._handle: TextIO | None = None
        self._disabled = False
        self._warning_emitted = False
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            self._handle = self.path.open("a", encoding="utf-8", buffering=1)
        except OSError as error:
            self._disable(error)

    def set_context(self, *, session_id: str | None = None, run_id: str | None = None) -> None:
        """Update identifiers included in subsequent records."""

        with self._lock:
            if session_id:
                self._session_id = session_id
            if run_id:
                self._run_id = run_id

    def record(
        self,
        kind: str,
        data: dict[str, object] | None = None,
        error: BaseException | None = None,
    ) -> None:
        """Append one diagnostic record, disabling logging after an I/O failure."""

        with self._lock:
            if self._disabled or self._handle is None:
                return
            self._sequence += 1
            record: dict[str, Any] = {
                "timestamp": datetime.now(UTC).isoformat(),
                "tui_id": self.tui_id,
                "sequence": self._sequence,
                "kind": kind,
                "session_id": self._session_id,
                "run_id": self._run_id,
                "data": dict(data or {}),
            }
            if error is not None:
                record["error"] = {
                    "type": f"{type(error).__module__}.{type(error).__qualname__}",
                    "message": str(error),
                    "traceback": "".join(traceback.format_exception(error)),
                }
            try:
                self._handle.write(json.dumps(record, ensure_ascii=False, default=str))
                self._handle.write("\n")
                self._handle.flush()
            except OSError as write_error:
                self._disable(write_error)

    def close(self) -> None:
        """Close the current diagnostics file."""

        with self._lock:
            handle = self._handle
            self._handle = None
            if handle is not None:
                try:
                    handle.close()
                except OSError as error:
                    self._disable(error)

    def _disable(self, error: OSError) -> None:
        self._disabled = True
        handle = self._handle
        self._handle = None
        if handle is not None:
            try:
                handle.close()
            except OSError:
                pass
        if not self._warning_emitted:
            self._warning_emitted = True
            try:
                self._warning_sink(f"TUI diagnostics disabled: {error}")
            except Exception:
                pass
