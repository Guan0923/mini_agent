"""Persistent JSON Lines sink for complete, machine-readable agent run logs."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mini_agent.runtime.events import RuntimeEvent


class JsonlRunLogger:
    """Append every event from a run to ``<log_dir>/<run_id>.jsonl``."""

    def __init__(self, log_dir: Path) -> None:
        self._log_dir = log_dir

    def __call__(self, event: RuntimeEvent) -> None:
        run_id = event.data.get("run_id")
        if not isinstance(run_id, str) or not run_id:
            raise ValueError("Persistent run logging requires a run_id in every event.")
        self._log_dir.mkdir(parents=True, exist_ok=True)
        record: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "run_id": run_id,
            "kind": event.kind,
            "message": event.message,
            "data": event.data,
        }
        with self.path_for(run_id).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, default=str))
            handle.write("\n")

    def path_for(self, run_id: str) -> Path:
        return self._log_dir / f"{run_id}.jsonl"
