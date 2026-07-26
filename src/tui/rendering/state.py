"""Runtime-event rendering and transcript state for the terminal view."""

from __future__ import annotations

from dataclasses import dataclass
from threading import Lock

from .mirror import TranscriptTextMirror
from .transcript import TranscriptNode, TranscriptScroll
from .transcript_content import MarkdownBody, ProcessingProgress, StatusLeaf

_FLUSH_INTERVAL_SECONDS = 1 / 30
OMITTED_MARKER = "[Earlier terminal output omitted]\n"
DETAIL_LEVELS = frozenset({"minimal", "medium", "verbose"})


@dataclass
class ToolTranscript:
    node: TranscriptNode
    arguments: MarkdownBody
    result: MarkdownBody
    status: StatusLeaf


class TranscriptStateMixin:
    """Own transcript buffering, structured nodes, and runtime-event rendering."""

    def _init_transcript_state(self, transcript_limit: int, transcript_node_limit: int, detail_level: str) -> None:
        if detail_level not in DETAIL_LEVELS:
            raise ValueError(f"Unknown transcript detail level: {detail_level}")
        self._transcript_limit = transcript_limit
        self._transcript_node_limit = transcript_node_limit
        self._detail_level = detail_level
        self._reconcile_scheduled = False
        self._pending_chunks: list[str] = []
        self._pending_lock = Lock()
        self._flush_scheduled = False
        self._pending_system_chunks: list[str] = []
        self._system_flush_scheduled = False
        self._transcript_mirror = TranscriptTextMirror()
        self.transcript = TranscriptScroll()
        self.transcript.set_text_source(self._transcript_mirror.snapshot)
        self.transcript_nodes: list[TranscriptNode] = []
        self.markdown_bodies: list[MarkdownBody] = []
        self._top_level_nodes: list[TranscriptNode] = []
        self._top_level_bodies: dict[TranscriptNode, list[MarkdownBody]] = {}
        self._node_top_level: dict[TranscriptNode, TranscriptNode] = {}
        self._completed_top_levels: set[TranscriptNode] = set()
        self._top_level_groups: dict[TranscriptNode, tuple[TranscriptNode, ...]] = {}
        self._pending_assistants: list[TranscriptNode] = []
        self._assistant_by_run: dict[str, TranscriptNode] = {}
        self._thinking_by_run: dict[str, tuple[TranscriptNode, MarkdownBody]] = {}
        self._response_by_run: dict[str, tuple[TranscriptNode, MarkdownBody]] = {}
        self._tools_by_call: dict[tuple[str, str], ToolTranscript] = {}
        self._seen_tool_calls: set[tuple[str, str]] = set()
        self._processing_by_run: dict[str, ProcessingProgress] = {}
        self._seen_exchanges: set[tuple[str, str]] = set()
        self._last_response_by_run: dict[str, str] = {}
        self._streaming_system: tuple[TranscriptNode, MarkdownBody] | None = None

    @property
    def detail_level(self) -> str:
        return self._detail_level

    def _set_detail_level(self, detail_level: str) -> None:
        if detail_level not in DETAIL_LEVELS:
            raise ValueError(f"Unknown transcript detail level: {detail_level}")
        self._detail_level = detail_level

    def _start_processing(self, run_id: str, assistant: TranscriptNode) -> None:
        if run_id in self._processing_by_run:
            return
        progress = ProcessingProgress()
        self._processing_by_run[run_id] = progress
        assistant.add_node(progress)
        self._scroll_after_transcript_change()

    def _finish_processing(self, run_id: str, failure_message: str | None = None) -> None:
        progress = self._processing_by_run.pop(run_id, None)
        if progress is None:
            return
        if failure_message is None:
            progress.complete()
        else:
            progress.fail(failure_message)

    def _schedule_flush(self) -> None:
        if self._writes_closed:
            return
        loop = self._owner_loop
        if loop is not None:
            loop.call_later(_FLUSH_INTERVAL_SECONDS, self._flush_pending)

    def _flush_pending(self) -> None:
        chunks = self._take_pending_chunks()
        if chunks:
            self._append_transcript("".join(chunks))

    def _take_pending_chunks(self) -> list[str]:
        with self._pending_lock:
            chunks = self._pending_chunks
            self._pending_chunks = []
            self._flush_scheduled = False
        return chunks

    def _append_transcript(self, value: str) -> None:
        old_scroll = self.transcript.scroll_y if self._follow_tail else self._paused_scroll_y
        combined = f"{self.transcript.text}{value}"
        if len(combined) > self._transcript_limit:
            keep = max(0, self._transcript_limit - len(OMITTED_MARKER))
            combined = f"{OMITTED_MARKER}{combined[-keep:]}" if keep else ""
            self.transcript.load_text(combined)
        else:
            self.transcript.append_text(value)
        self.call_after_refresh(self._sync_transcript_scroll, old_scroll)

    def _new_top_level(self, title: str, *, completed: bool = False) -> TranscriptNode:
        role = title.casefold()
        classes = (
            f"transcript-node transcript-role transcript-{role}" if role in {"user", "assistant"} else "transcript-node"
        )
        node = TranscriptNode(title, collapsed=False, classes=classes)
        self.transcript_nodes.append(node)
        self._top_level_nodes.append(node)
        self._top_level_bodies[node] = []
        self._node_top_level[node] = node
        self._top_level_groups[node] = (node,)
        if completed:
            self._completed_top_levels.add(node)
        mirror_title = None if node.has_class("transcript-role") else node.title_text
        self._transcript_mirror.add_top_level(node, mirror_title)
        self.transcript.add_top_level(node)
        self._scroll_after_transcript_change()
        return node

    def _add_assistant_node(
        self,
        assistant: TranscriptNode,
        title: str,
        *,
        collapsed: bool,
        markdown: str = "",
    ) -> tuple[TranscriptNode, MarkdownBody]:
        body = MarkdownBody(markdown)
        node = TranscriptNode(title, body, collapsed=collapsed)
        self._register_body(body, assistant)
        self.transcript_nodes.append(node)
        self._node_top_level[node] = assistant
        assistant.add_node(node)
        self._scroll_after_transcript_change()
        return node, body

    def _register_body(self, body: MarkdownBody, top_level: TranscriptNode) -> None:
        self.markdown_bodies.append(body)
        self._top_level_bodies[top_level].append(body)
        self._transcript_mirror.add_body(top_level, body, body.markdown_text)
        body.set_source_listener(self._on_body_text_changed)

    def _on_body_text_changed(self, body: MarkdownBody, _old: str, new: str) -> None:
        if self._transcript_mirror.update_body(body, new):
            self._scroll_after_transcript_change()

    def _assistant_for_run(self, run_id: str) -> TranscriptNode | None:
        assistant = self._assistant_by_run.get(run_id)
        if assistant is None and self._pending_assistants:
            assistant = self._pending_assistants.pop(0)
            self._assistant_by_run[run_id] = assistant
        return assistant

    def _append_system_output(self, value: str) -> None:
        if self._streaming_system is None:
            system = self._new_top_level("SYSTEM")
            body = MarkdownBody("")
            self._register_body(body, system)
            system.add_node(body)
            self._streaming_system = (system, body)
        self._streaming_system[1].append_markdown(value)
        if value.endswith("\n"):
            self._completed_top_levels.add(self._streaming_system[0])
            self._streaming_system = None

    def _flush_system_output(self) -> None:
        self._system_flush_scheduled = False
        if not self._pending_system_chunks:
            return
        value = "".join(self._pending_system_chunks)
        self._pending_system_chunks = []
        self._append_system_output(value)
        self._scroll_after_transcript_change()
