"""Runtime-event rendering and transcript state for the terminal view."""

from __future__ import annotations

import json
from dataclasses import dataclass
from threading import Lock

from mini_agent.runtime.core.events import RuntimeEvent

from .transcript import MarkdownBody, StatusLeaf, TranscriptNode, TranscriptScroll

_FLUSH_INTERVAL_SECONDS = 1 / 30
_OMITTED_MARKER = "[Earlier terminal output omitted]\n"


@dataclass
class ToolTranscript:
    node: TranscriptNode
    arguments: MarkdownBody
    result: MarkdownBody
    status: StatusLeaf


class TranscriptRenderingMixin:
    """Own transcript buffering, structured nodes, and runtime-event rendering."""

    def _init_transcript_state(self, transcript_limit: int, transcript_node_limit: int) -> None:
        self._transcript_limit = transcript_limit
        self._transcript_node_limit = transcript_node_limit
        self._reconcile_scheduled = False
        self._pending_chunks: list[str] = []
        self._pending_lock = Lock()
        self._flush_scheduled = False
        self._pending_system_chunks: list[str] = []
        self._system_flush_scheduled = False
        self.transcript = TranscriptScroll()
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
        self._seen_exchanges: set[tuple[str, str]] = set()
        self._last_response_by_run: dict[str, str] = {}
        self._streaming_system: tuple[TranscriptNode, MarkdownBody] | None = None

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
            keep = max(0, self._transcript_limit - len(_OMITTED_MARKER))
            combined = f"{_OMITTED_MARKER}{combined[-keep:]}" if keep else ""
            self.transcript.load_text(combined)
        else:
            self.transcript.append_text(value)
        self.call_after_refresh(self._sync_transcript_scroll, old_scroll)

    def _new_top_level(self, title: str, *, completed: bool = False) -> TranscriptNode:
        node = TranscriptNode(title, collapsed=title == "SYSTEM")
        self.transcript_nodes.append(node)
        self._top_level_nodes.append(node)
        self._top_level_bodies[node] = []
        self._node_top_level[node] = node
        self._top_level_groups[node] = (node,)
        if completed:
            self._completed_top_levels.add(node)
        self.transcript.add_top_level(node)
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
        return node, body

    def _register_body(self, body: MarkdownBody, top_level: TranscriptNode) -> None:
        self.markdown_bodies.append(body)
        self._top_level_bodies[top_level].append(body)

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

    def _handle_runtime_event_now(self, event: RuntimeEvent) -> None:
        self._flush_system_output()
        data = event.data
        run_id = str(data.get("run_id", ""))
        if event.kind == "run_started":
            if run_id:
                self._assistant_for_run(run_id)
            return
        if not run_id:
            return
        assistant = self._assistant_for_run(run_id)
        if assistant is None:
            return
        self._streaming_system = None
        if event.kind == "thinking_start":
            node, body = self._add_assistant_node(assistant, "think_content", collapsed=True)
            node.set_activity(True)
            self._thinking_by_run[run_id] = (node, body)
        elif event.kind == "thinking_delta":
            current = self._thinking_by_run.get(run_id)
            if current is not None:
                current[1].append_markdown(event.message)
        elif event.kind == "thinking_end":
            current = self._thinking_by_run.pop(run_id, None)
            if current is not None:
                current[0].set_activity(False)
        elif event.kind == "response_start":
            node, body = self._add_assistant_node(assistant, "response_content", collapsed=False)
            node.set_activity(True)
            self._response_by_run[run_id] = (node, body)
        elif event.kind == "response_delta":
            current = self._response_by_run.get(run_id)
            if current is not None:
                current[1].append_markdown(event.message)
                self._last_response_by_run[run_id] = current[1].markdown_text
        elif event.kind == "response_end":
            current = self._response_by_run.pop(run_id, None)
            if current is not None:
                current[0].set_activity(False)
                self._last_response_by_run[run_id] = current[1].markdown_text
        elif event.kind == "assistant_message":
            self._handle_assistant_message(run_id, assistant, data)
        elif event.kind == "tool_call":
            self._handle_tool_call(run_id, assistant, event.message, data)
        elif event.kind in {"tool_result", "tool_failed"}:
            self._handle_tool_completion(run_id, assistant, event.kind, event.message, data)
        elif event.kind in {"retry", "tool_recovery"}:
            tool = self._tool_for_event(run_id, assistant, event.message, data)
            if tool is not None:
                tool.status.set_status("running")
                tool.node.set_activity(True)
        elif event.kind in {"response", "plan"}:
            if event.message != self._last_response_by_run.get(run_id, ""):
                _, body = self._add_assistant_node(
                    assistant, "response_content", collapsed=False, markdown=event.message
                )
                self._last_response_by_run[run_id] = body.markdown_text
        elif event.kind == "run_finished":
            self._stop_run_activity(run_id)
            self._completed_top_levels.add(assistant)
        elif event.kind in {"cancelled", "error", "model_error"}:
            self._stop_run_activity(run_id)
            if event.message:
                self._add_assistant_node(assistant, event.kind, collapsed=False, markdown=event.message)
            self._completed_top_levels.add(assistant)
        elif event.kind not in {"model_request", "model_response", "context_usage", "strategy"} and event.message:
            self._add_assistant_node(assistant, event.kind, collapsed=False, markdown=event.message)
        self._scroll_after_transcript_change()

    def _handle_assistant_message(
        self, run_id: str, assistant: TranscriptNode, data: dict[str, object]
    ) -> None:
        exchange_id = data.get("exchange_id")
        if isinstance(exchange_id, str) and (run_id, exchange_id) in self._seen_exchanges:
            return
        if isinstance(exchange_id, str):
            self._seen_exchanges.add((run_id, exchange_id))
        message = data.get("message")
        if not isinstance(message, dict):
            return
        reasoning = message.get("reasoning")
        if isinstance(reasoning, str) and reasoning and not data.get("reasoning_streamed"):
            self._add_assistant_node(assistant, "think_content", collapsed=True, markdown=reasoning)
        content = message.get("content")
        if isinstance(content, str) and content and not data.get("content_streamed"):
            if content != self._last_response_by_run.get(run_id, ""):
                _, body = self._add_assistant_node(
                    assistant, "response_content", collapsed=False, markdown=content
                )
                self._last_response_by_run[run_id] = body.markdown_text
        tools = message.get("tool_messages", ())
        if isinstance(tools, list):
            for tool_data in tools:
                if isinstance(tool_data, dict):
                    self._ensure_tool(run_id, assistant, tool_data)

    def _ensure_tool(
        self, run_id: str, assistant: TranscriptNode, data: dict[str, object]
    ) -> ToolTranscript | None:
        call_id = data.get("call_id")
        if not isinstance(call_id, str) or not call_id:
            return None
        key = (run_id, call_id)
        existing = self._tools_by_call.get(key)
        if existing is not None:
            return existing
        name = str(data.get("name") or data.get("tool") or "tool")
        arguments = data.get("arguments", {})
        formatted = json.dumps(arguments, ensure_ascii=False, indent=2, default=str)
        argument_body = MarkdownBody(f"```json\n{formatted}\n```")
        result_body = MarkdownBody("")
        arguments_node = TranscriptNode("arguments", argument_body, collapsed=False)
        result_node = TranscriptNode("result", result_body, collapsed=False)
        status = StatusLeaf(str(data.get("status", "pending")))
        node = TranscriptNode(
            f"tool_call: {name}", arguments_node, result_node, status, collapsed=True
        )
        self._register_body(argument_body, assistant)
        self._register_body(result_body, assistant)
        self.transcript_nodes.append(node)
        self._node_top_level[node] = assistant
        assistant.add_node(node)
        tool = ToolTranscript(node, argument_body, result_body, status)
        self._tools_by_call[key] = tool
        node.set_activity(status.status in {"pending", "running"})
        return tool

    def _tool_for_event(
        self, run_id: str, assistant: TranscriptNode, message: str, data: dict[str, object]
    ) -> ToolTranscript | None:
        call_id = data.get("call_id")
        if isinstance(call_id, str) and call_id:
            tool = self._tools_by_call.get((run_id, call_id))
            if tool is not None:
                return tool
            details = dict(data)
            details.setdefault("name", message or data.get("tool", "tool"))
            return self._ensure_tool(run_id, assistant, details)
        return None

    def _handle_tool_call(
        self, run_id: str, assistant: TranscriptNode, message: str, data: dict[str, object]
    ) -> None:
        tool = self._tool_for_event(run_id, assistant, message, data)
        if tool is None:
            return
        if "arguments" in data:
            formatted = json.dumps(data["arguments"], ensure_ascii=False, indent=2, default=str)
            tool.arguments.set_markdown(f"```json\n{formatted}\n```")
        tool.status.set_status("running")
        tool.node.set_activity(True)

    def _handle_tool_completion(
        self,
        run_id: str,
        assistant: TranscriptNode,
        kind: str,
        message: str,
        data: dict[str, object],
    ) -> None:
        tool = self._tool_for_event(run_id, assistant, str(data.get("tool", "tool")), data)
        if tool is None:
            return
        tool.result.set_markdown(message)
        tool.status.set_status("succeeded" if kind == "tool_result" else "failed")
        tool.node.set_activity(False)

    def _stop_run_activity(self, run_id: str) -> None:
        current = self._thinking_by_run.pop(run_id, None)
        if current is not None:
            current[0].set_activity(False)
        current = self._response_by_run.pop(run_id, None)
        if current is not None:
            current[0].set_activity(False)
        for (tool_run_id, _), tool in self._tools_by_call.items():
            if tool_run_id == run_id:
                tool.node.set_activity(False)

    def _reset_transcript_state(self) -> None:
        self.transcript.clear_nodes(self._top_level_nodes)
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
        self._seen_exchanges = set()
        self._last_response_by_run = {}
        self._streaming_system = None
        self._pending_system_chunks = []
        self._system_flush_scheduled = False

    def _scroll_after_transcript_change(self) -> None:
        if self._reconcile_scheduled:
            return
        self._reconcile_scheduled = True
        self.call_after_refresh(self._reconcile_transcript)

    def _reconcile_transcript(self) -> None:
        self._reconcile_scheduled = False
        self._trim_completed_top_levels()
        self.transcript.sync_text(self._structured_transcript_text())
        if self._follow_tail:
            self.transcript.scroll_end(animate=False)
    def _structured_transcript_text(self) -> str:
        sections: list[str] = []
        for node in self._top_level_nodes:
            sections.append(node.title_text)
            sections.extend(
                body.markdown_text
                for body in self._top_level_bodies.get(node, ())
                if body.markdown_text
            )
        return "\n".join(sections)

    def _trim_completed_top_levels(self) -> None:
        structured = self._structured_transcript_text()
        while (
            len(structured) > self._transcript_limit
            or len(self.transcript_nodes) > self._transcript_node_limit
        ):
            group = next(
                (
                    self._top_level_groups.get(node, (node,))
                    for node in self._top_level_nodes
                    if all(
                        member in self._completed_top_levels
                        for member in self._top_level_groups.get(node, (node,))
                    )
                ),
                None,
            )
            if group is None:
                return
            for candidate in group:
                if candidate in self._top_level_nodes:
                    self._remove_top_level(candidate)
            structured = self._structured_transcript_text()

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
        self.markdown_bodies = [body for body in self.markdown_bodies if body not in removed_bodies]
        removed_nodes = {
            child for child, top_level in self._node_top_level.items() if top_level is node
        }
        self.transcript_nodes = [child for child in self.transcript_nodes if child not in removed_nodes]
        for child in removed_nodes:
            self._node_top_level.pop(child, None)
        self._pending_assistants = [assistant for assistant in self._pending_assistants if assistant is not node]
        removed_runs = [run_id for run_id, assistant in self._assistant_by_run.items() if assistant is node]
        for run_id in removed_runs:
            self._assistant_by_run.pop(run_id, None)
            self._thinking_by_run.pop(run_id, None)
            self._response_by_run.pop(run_id, None)
            self._last_response_by_run.pop(run_id, None)
            self._seen_exchanges = {item for item in self._seen_exchanges if item[0] != run_id}
            self._tools_by_call = {
                key: tool for key, tool in self._tools_by_call.items() if key[0] != run_id
            }

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
