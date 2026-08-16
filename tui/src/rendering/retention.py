"""Transcript reconciliation, retention, and clearing."""

from __future__ import annotations

from .transcript import TranscriptNode


class TranscriptRetentionMixin:
    def _reset_transcript_state(self) -> None:
        progress = getattr(self, "_compact_progress", None)
        if progress is not None:
            progress.stop()
        for processing in self._processing_by_run.values():
            processing.stop()
        self.transcript.clear_nodes(self._top_level_nodes)
        for body in self.markdown_bodies:
            body.set_source_listener(None)
        self._transcript_mirror.clear()
        self.transcript_nodes = []
        self.markdown_bodies = []
        self._top_level_nodes = []
        self._top_level_bodies = {}
        self._node_top_level = {}
        self._completed_top_levels = set()
        self._top_level_groups = {}
        self._reconcile_scheduled = False
        self._pending_assistants = []
        self._assistant_by_run = {}
        self._thinking_by_run = {}
        self._response_by_run = {}
        self._tools_by_call = {}
        self._seen_tool_calls = set()
        self._processing_by_run = {}
        self._seen_exchanges = set()
        self._last_response_by_run = {}
        self._streaming_system = None
        self._pending_system_chunks = []
        self._system_flush_scheduled = False
        self._compact_node = None
        self._compact_progress = None

    def _scroll_after_transcript_change(self) -> None:
        if self._reconcile_scheduled:
            return
        self._reconcile_scheduled = True
        self.call_after_refresh(self._reconcile_transcript)

    def _reconcile_transcript(self) -> None:
        self._reconcile_scheduled = False
        self._trim_completed_top_levels()
        if self._follow_tail:
            self.transcript.scroll_end(animate=False)

    def _structured_transcript_text(self) -> str:
        return self._transcript_mirror.text

    def _trim_completed_top_levels(self) -> None:
        while (
            self._transcript_mirror.length > self._transcript_limit
            or len(self.transcript_nodes) > self._transcript_node_limit
        ):
            group = next(
                (
                    self._top_level_groups.get(node, (node,))
                    for node in self._top_level_nodes
                    if all(member in self._completed_top_levels for member in self._top_level_groups.get(node, (node,)))
                ),
                None,
            )
            if group is None:
                return
            for candidate in group:
                if candidate in self._top_level_nodes:
                    self._remove_top_level(candidate)

    def _remove_top_level(self, node: TranscriptNode) -> None:
        if node.is_mounted:
            node.remove()
        self._top_level_nodes.remove(node)
        self._completed_top_levels.discard(node)
        group = self._top_level_groups.pop(node, (node,))
        for member in group:
            if member is not node:
                self._top_level_groups.pop(member, None)
        removed_bodies = set(self._top_level_bodies.pop(node, ()))
        for body in removed_bodies:
            body.set_source_listener(None)
        self._transcript_mirror.remove_top_level(node)
        self.markdown_bodies = [body for body in self.markdown_bodies if body not in removed_bodies]
        removed_nodes = {child for child, top_level in self._node_top_level.items() if top_level is node}
        self.transcript_nodes = [child for child in self.transcript_nodes if child not in removed_nodes]
        for child in removed_nodes:
            self._node_top_level.pop(child, None)
        self._pending_assistants = [assistant for assistant in self._pending_assistants if assistant is not node]
        removed_runs = [run_id for run_id, assistant in self._assistant_by_run.items() if assistant is node]
        for run_id in removed_runs:
            processing = self._processing_by_run.pop(run_id, None)
            if processing is not None:
                processing.stop()
            self._assistant_by_run.pop(run_id, None)
            self._thinking_by_run.pop(run_id, None)
            self._response_by_run.pop(run_id, None)
            self._last_response_by_run.pop(run_id, None)
            self._seen_exchanges = {item for item in self._seen_exchanges if item[0] != run_id}
            self._seen_tool_calls = {item for item in self._seen_tool_calls if item[0] != run_id}
            self._tools_by_call = {key: tool for key, tool in self._tools_by_call.items() if key[0] != run_id}

    def _sync_transcript_scroll(self, previous_scroll: float) -> None:
        if self._follow_tail:
            self.transcript.scroll_end(animate=False)

    def _clear_now(self) -> None:
        with self._pending_lock:
            self._pending_chunks = []
            self._flush_scheduled = False
        self._pending_system_chunks: list[str] = []
        self._system_flush_scheduled = False
        self._reset_transcript_state()
        self.transcript.load_text("")
        self._follow_tail = True
        self._paused_scroll_y = 0.0
        self._refresh_status()
