"""JSON normalization, streamed Items, usage, and runtime configuration."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from typing import Any

from backend.domain import TracePersistenceError
from backend.domain.runtime_state import RuntimeState, RuntimeStateValidationError
from backend.providers.token_usage import normalize_provider_usage
from backend.runtime.persistence.recording import turn_trace_audit_value


class _ItemProjectionMixin:
    @staticmethod
    def _json_value(value: Any) -> Any:
        if value is None or isinstance(value, (str, bool, int)):
            return value
        if isinstance(value, float):
            return value if math.isfinite(value) else str(value)
        if isinstance(value, Mapping):
            return {str(key): _ItemProjectionMixin._json_value(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [_ItemProjectionMixin._json_value(item) for item in value]
        try:
            json.dumps(value, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError):
            return str(value)
        return value

    def _begin_stream_item(self, item_type: str) -> None:
        if item_type not in {"reasoning", "text"}:
            raise ValueError("Only reasoning and text Items can stream.")
        if self._stream_item_type == item_type:
            return
        self._finish_stream_item()
        self._stream_item_type = item_type
        self._stream_item_index = None
        self._stream_text = ""

    def _update_stream_item(self, item_type: str, chunk: str) -> None:
        if not chunk:
            return
        if self._stream_item_type != item_type:
            self._begin_stream_item(item_type)
        self._ensure_assistant_message()
        if self._stream_item_index is None:
            self._stream_item_index = len(self.assistant_blocks)
            self._stream_text = chunk
            item = {"type": item_type, "text": chunk, "status": "running"}
            self.assistant_blocks.append(item)
            assert self.assistant is not None
            self.assistant = self.writer.append_items(
                self.assistant,
                [item],
                message_idx=self.assistant_message_idx,
                persist=False,
            )
        else:
            self._stream_text += chunk
            self.assistant_blocks[self._stream_item_index] = {
                "type": item_type,
                "text": self._stream_text,
                "status": "running",
            }
            assert self.assistant is not None
            self.assistant = self.writer.append_text(
                self.assistant,
                data_idx=self.assistant.current_data_idx,
                message_idx=self.assistant_message_idx,
                item_idx=self._stream_item_index,
                delta=chunk,
            )
        self.last_node = self.assistant

    def _finish_stream_item(self, item_type: str | None = None, *, status: str = "success") -> None:
        if self._stream_item_type is None:
            return
        if item_type is not None and self._stream_item_type != item_type:
            return
        if self._stream_item_index is not None:
            assert self.assistant is not None
            assert self.assistant_message_idx is not None
            self.assistant = self.writer.persist(self.assistant)
            self.assistant = self.writer.set_item_status(
                self.assistant,
                data_idx=self.assistant.current_data_idx,
                message_idx=self.assistant_message_idx,
                item_idx=self._stream_item_index,
                status=status,
            )
            self.assistant_blocks[self._stream_item_index]["status"] = status
            self.last_node = self.assistant
            self.produced_item = True
            self._record_completed_item(self.assistant_message_idx, self._stream_item_index)
        self._stream_item_index = None
        self._stream_item_type = None
        self._stream_text = ""

    def _append_item(self, item: Mapping[str, Any], *, persist: bool = True) -> RuntimeState:
        self._finish_stream_item()
        self._ensure_assistant_message()
        normalized = {str(key): self._json_value(value) for key, value in item.items()}
        normalized.setdefault("status", "success")
        item_idx = len(self.assistant_blocks)
        self.assistant_blocks.append(normalized)
        if self.assistant is None:
            raise RuntimeError("No active Turn.")
        updated = self.writer.append_items(
            self.assistant,
            [normalized],
            message_idx=self.assistant_message_idx,
            persist=persist,
        )
        self.assistant = updated
        self.last_node = updated
        if persist:
            self.produced_item = True
            if normalized.get("status") in {"success", "failed"}:
                assert self.assistant_message_idx is not None
                self._record_completed_item(self.assistant_message_idx, item_idx)
        return updated

    def _append_items(self, items: Sequence[Mapping[str, Any]]) -> RuntimeState | None:
        self._finish_stream_item()
        if not items:
            return self.assistant
        self._ensure_assistant_message()
        first_item_idx = len(self.assistant_blocks)
        normalized = []
        for item in items:
            value = {str(key): self._json_value(raw) for key, raw in item.items()}
            value.setdefault("status", "success")
            normalized.append(value)
        self.assistant_blocks.extend(normalized)
        if self.assistant is None:
            raise RuntimeError("No active Turn.")
        updated = self.writer.append_items(
            self.assistant,
            normalized,
            message_idx=self.assistant_message_idx,
            persist=True,
        )
        self.assistant = updated
        self.last_node = updated
        self.produced_item = True
        assert self.assistant_message_idx is not None
        for offset, item in enumerate(normalized):
            if item.get("status") in {"success", "failed"}:
                self._record_completed_item(self.assistant_message_idx, first_item_idx + offset)
        return updated

    def _record_completed_item(self, message_idx: int, item_idx: int) -> None:
        """Persist one completed canonical Item after its Turn write succeeds."""

        if self.trace_persistence_failed or self.runtime is None or self.assistant is None:
            return
        services = self.runtime.services
        if not services.turn_trace_initialized:
            return
        append = getattr(self.store, "append_turn_trace_item", None)
        try:
            if not callable(append):
                raise RuntimeError("Turn Trace Item persistence is unavailable.")
            data_idx = self.assistant.current_data_idx
            message = self.assistant.data[data_idx][message_idx]
            raw_item = message["content"][item_idx]
            if raw_item.get("status") not in {"success", "failed"}:
                return
            audited = turn_trace_audit_value(raw_item)
            if not isinstance(audited, Mapping):
                raise RuntimeError("Turn Trace Item is not a JSON object.")
            stored = append(
                self.assistant.session_id,
                self.assistant.id,
                data_idx,
                message_idx=message_idx,
                item_idx=item_idx,
                role=str(message.get("role") or "assistant"),
                item=dict(audited),
                completed_at=services.clock(),
            )
            if stored is None:
                raise RuntimeError("Turn Trace was not initialized.")
        except TracePersistenceError:
            self.trace_persistence_failed = True
            raise
        except Exception as exc:
            self.trace_persistence_failed = True
            raise TracePersistenceError("Local trace Item persistence failed; the Turn was stopped.") from exc

    def _apply_usage(self, raw: Any) -> None:
        if not isinstance(raw, Mapping) or self.assistant is None:
            return
        normalized = normalize_provider_usage(raw)
        current = self.writer.current(self.assistant.session_id, self.assistant.id)
        merged = {key: value if value is not None else current.usage.get(key) for key, value in normalized.items()}
        self.assistant = self.writer.update_config(current, usage=merged)
        self.last_node = self.assistant

    def apply_runtime_config(self, config: Mapping[str, Any]) -> RuntimeState | None:
        """Merge one PATCH atomically and queue it for the next runtime boundary."""

        with self._runtime_config_lock:
            return self._apply_runtime_config_unlocked(config)

    def _apply_runtime_config_unlocked(self, config: Mapping[str, Any]) -> RuntimeState | None:
        provider_name = str(config.get("provider_name") or self.provider_name)
        if not provider_name.strip():
            raise RuntimeStateValidationError("provider_name must be a non-empty string.")
        model = dict(self.model_config)
        if isinstance(config.get("model"), Mapping):
            model.update(dict(config["model"]))
        permission = str(config.get("permission_mode") or self.permission_mode)
        running = str(config.get("running_mode") or self.running_mode)
        if permission not in {"read_only", "workspace_write", "full_access"}:
            raise RuntimeStateValidationError("permission_mode must be read_only, workspace_write, or full_access.")
        if running not in {"agent", "plan"}:
            raise RuntimeStateValidationError("running_mode must be agent or plan.")
        self.provider_name, self.model_config = provider_name, model
        self.permission_mode, self.running_mode = permission, running
        if self.assistant is None:
            return self.last_node
        self.assistant = self.writer.update_config(
            self.assistant, provider_name=provider_name, model=model, permission_mode=permission, running_mode=running
        )
        self.last_node = self.assistant
        if self.runtime is not None:
            pending = dict(self.runtime.services.pending_runtime_config or {})
            pending.update(dict(config))
            self.runtime.services.pending_runtime_config = pending
        return self.assistant
