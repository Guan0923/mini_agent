"""Persistent JSON Lines sink for complete, machine-readable agent run logs."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from mini_agent.runtime.core.events import RuntimeEvent
from mini_agent.runtime.persistence.recording import LOG_SCHEMA_VERSION, persistent_event


class JsonlRunLogger:
    """Append durable run events to the per-run JSONL file."""

    def __init__(self, log_dir: Path, include_full_messages: bool = True) -> None:
        self._log_dir = log_dir
        self._include_full_messages = include_full_messages
        self._thinking: dict[str, tuple[str, dict[str, Any], list[str]]] = {}
        self._sequences: dict[str, int] = {}

    def __call__(self, event: RuntimeEvent) -> None:
        run_id = event.data.get("run_id")
        if not isinstance(run_id, str) or not run_id:
            raise ValueError("Persistent run logging requires a run_id in every event.")
        if event.kind in {"response_start", "response_delta", "response_end"}:
            return
        if event.kind == "assistant_message":
            self._write_non_stream_reasoning(event)
            return
        if event.kind == "thinking_start":
            self._thinking[run_id] = (event.timestamp, dict(event.data), [])
            return
        if event.kind == "thinking_delta":
            timestamp, data, chunks = self._thinking.get(run_id, (event.timestamp, dict(event.data), []))
            chunks.append(event.message)
            self._thinking[run_id] = (timestamp, data, chunks)
            return
        if event.kind == "thinking_end":
            self._flush_thinking(run_id, event.data, completed=True)
            return
        self._flush_thinking(run_id, event.data, completed=False)
        self._write(event)

    def _write_non_stream_reasoning(self, event: RuntimeEvent) -> None:
        if event.data.get("reasoning_streamed"):
            return
        message = event.data.get("message")
        if not isinstance(message, dict):
            return
        reasoning = message.get("reasoning")
        if not isinstance(reasoning, str) or not reasoning:
            return
        data = {key: event.data[key] for key in ("run_id", "task", "mode", "strategy", "status") if key in event.data}
        data["streamed"] = False
        self._write(RuntimeEvent("thinking_delta", reasoning, data, timestamp=event.timestamp), kind="thinking")

    def _flush_thinking(self, run_id: str, fallback_data: dict[str, Any], *, completed: bool = False) -> None:
        buffered = self._thinking.pop(run_id, None)
        if buffered is None:
            return
        timestamp, data, chunks = buffered
        if completed:
            persistent_data = {"streamed": True, **fallback_data}
        else:
            persistent_data = {"streamed": True, **(data or dict(fallback_data)), "interrupted": True}
        event = RuntimeEvent("thinking_delta", "".join(chunks), persistent_data, timestamp=timestamp)
        self._write(event, kind="thinking")

    def _write(self, event: RuntimeEvent, *, kind: str | None = None) -> None:
        run_id = event.data["run_id"]
        assert isinstance(run_id, str)
        sequence = self._sequences.get(run_id, 0) + 1
        self._sequences[run_id] = sequence
        message, data = persistent_event(event, self._include_full_messages)
        self._log_dir.mkdir(parents=True, exist_ok=True)
        record: dict[str, Any] = {
            "schema_version": LOG_SCHEMA_VERSION,
            "timestamp": event.timestamp,
            "run_id": run_id,
            "sequence": sequence,
            "kind": kind or event.kind,
            "message": message,
            "data": data,
        }
        with self.path_for(run_id).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, default=str))
            handle.write("\n")

    def path_for(self, run_id: str) -> Path:
        return self._log_dir / f"{run_id}.jsonl"
