"""Diagnostic helpers mixed into the Textual terminal view."""

from __future__ import annotations

from threading import get_ident
from time import monotonic

from mini_agent.runtime.core.events import RuntimeEvent


class TuiDiagnosticMixin:
    """Collect bounded UI state without coupling diagnostics to rendering."""

    def diagnostic_snapshot(self) -> dict[str, object]:
        """Return bounded UI state for crash reports without transcript content."""

        with self._diagnostic_lock:
            last_event = dict(self._last_runtime_event)
        run_id = str(last_event.get("run_id") or "")
        active_response = self._response_by_run.get(run_id)
        if active_response is not None:
            response_node, response_body = active_response
            response_chars = len(response_body.markdown_text)
            response_active = True
            response_node_mounted = response_node.is_mounted
            response_body_mounted = response_body.is_mounted
        else:
            response_chars = len(self._last_response_by_run.get(run_id, "")) if run_id else 0
            response_active = False
            response_node_mounted = False
            response_body_mounted = False
        return {
            "owner_ready": self._owner_ready,
            "app_exit_requested": self._exit,
            "message_pump_closed": self._closed,
            "message_pump_closing": self._closing,
            "textual_return_code": self.return_code,
            "owner_thread_id": self._thread_id,
            "snapshot_thread_id": get_ident(),
            "is_running": self.is_running,
            "writes_closed": self._writes_closed,
            "pending_owner_callbacks": len(self._pending_owner_callbacks),
            "reconcile_scheduled": self._reconcile_scheduled,
            "top_level_nodes": len(self._top_level_nodes),
            "transcript_nodes": len(self.transcript_nodes),
            "markdown_bodies": len(self.markdown_bodies),
            "response_active": response_active,
            "response_chars": response_chars,
            "response_node_mounted": response_node_mounted,
            "response_body_mounted": response_body_mounted,
            "last_runtime_event": last_event,
        }

    @property
    def unhandled_exception(self) -> BaseException | None:
        """Expose Textual's captured exception before the view is discarded."""

        return self._exception

    def _runtime_event_metadata(self, event: RuntimeEvent) -> dict[str, object]:
        metadata: dict[str, object] = {
            "event_kind": event.kind,
            "run_id": str(event.data.get("run_id") or ""),
            "event_timestamp": event.timestamp,
            "message_chars": len(event.message),
            "source_thread_id": get_ident(),
        }
        call_id = event.data.get("call_id")
        if isinstance(call_id, str) and call_id:
            metadata["call_id"] = call_id
        with self._diagnostic_lock:
            self._last_runtime_event = dict(metadata)
        return metadata

    def _update_stream_diagnostics(self, event: RuntimeEvent) -> None:
        run_id = str(event.data.get("run_id") or "")
        stream_kind = "response_delta" if event.kind.startswith("response_") else "thinking_delta"
        key = (run_id, stream_kind)
        now = monotonic()
        progress: dict[str, object] | None = None
        with self._diagnostic_lock:
            if event.kind in {"response_start", "thinking_start"}:
                self._stream_diagnostics[key] = (0, 0, now)
                return
            if event.kind in {"response_delta", "thinking_delta"}:
                chunks, characters, last_written = self._stream_diagnostics.get(key, (0, 0, now))
                chunks += 1
                characters += len(event.message)
                self._stream_diagnostics[key] = (chunks, characters, last_written)
                if now - last_written >= 0.25:
                    self._stream_diagnostics[key] = (chunks, characters, now)
                    progress = {
                        "stream_kind": stream_kind,
                        "run_id": run_id,
                        "chunks": chunks,
                        "characters": characters,
                        "final": False,
                    }
            elif event.kind in {"response_end", "thinking_end"}:
                chunks, characters, _last_written = self._stream_diagnostics.pop(key, (0, 0, now))
                progress = {
                    "stream_kind": stream_kind,
                    "run_id": run_id,
                    "chunks": chunks,
                    "characters": characters,
                    "final": True,
                }
        if progress is not None:
            self._diagnose("runtime_stream_progress", progress)

    def _diagnose(
        self,
        kind: str,
        data: dict[str, object] | None = None,
        error: BaseException | None = None,
    ) -> None:
        sink = self._diagnostic_sink
        if sink is None:
            return
        try:
            sink(kind, data, error)
        except Exception:
            pass
