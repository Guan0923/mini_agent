"""Map runtime events into structured transcript nodes."""

from __future__ import annotations

import json

from textual.widgets import Static

from backend.runtime.core.events import RuntimeEvent

from .state import ToolTranscript
from .transcript import TranscriptNode
from .transcript_content import MarkdownBody, StatusLeaf


class TranscriptEventMixin:
    def _handle_runtime_event_now(self, event: RuntimeEvent) -> None:
        data = event.data
        run_id = str(data.get("run_id", ""))
        if event.kind == "run_started":
            if run_id:
                assistant = self._assistant_for_run(run_id)
                if assistant is not None and self.detail_level == "minimal":
                    self._start_processing(run_id, assistant)
            return
        if not run_id:
            return
        assistant = self._assistant_for_run(run_id)
        if assistant is None:
            return
        if event.kind == "thinking_start":
            if self.detail_level == "minimal":
                return
            node, body = self._add_assistant_node(assistant, "think_content", collapsed=False)
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
                current[0].collapsed = True
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
            self._finish_processing(run_id)
            self._completed_top_levels.add(assistant)
            self._scroll_after_transcript_change()
        elif event.kind in {"cancelled", "error", "model_error"}:
            self._stop_run_activity(run_id)
            self._finish_processing(run_id, event.message or "已取消")
            if event.message:
                self._add_assistant_node(assistant, event.kind, collapsed=False, markdown=event.message)
            self._completed_top_levels.add(assistant)
            self._scroll_after_transcript_change()
        elif (
            event.kind
            not in {
                "model_request",
                "model_response",
                "model_repair",
                "context_usage",
                "strategy",
            }
            and event.message
        ):
            self._add_assistant_node(assistant, event.kind, collapsed=False, markdown=event.message)

    def _handle_assistant_message(self, run_id: str, assistant: TranscriptNode, data: dict[str, object]) -> None:
        exchange_id = data.get("exchange_id")
        if isinstance(exchange_id, str) and (run_id, exchange_id) in self._seen_exchanges:
            return
        if isinstance(exchange_id, str):
            self._seen_exchanges.add((run_id, exchange_id))
        message = data.get("message")
        if not isinstance(message, dict):
            return
        reasoning = message.get("reasoning")
        if (
            self.detail_level != "minimal"
            and isinstance(reasoning, str)
            and reasoning
            and not data.get("reasoning_streamed")
        ):
            self._add_assistant_node(assistant, "think_content", collapsed=True, markdown=reasoning)
        content = message.get("content")
        if isinstance(content, str) and content and not data.get("content_streamed"):
            if content != self._last_response_by_run.get(run_id, ""):
                _, body = self._add_assistant_node(assistant, "response_content", collapsed=False, markdown=content)
                self._last_response_by_run[run_id] = body.markdown_text
        tools = message.get("tool_messages", ())
        if isinstance(tools, list):
            for tool_data in tools:
                if isinstance(tool_data, dict):
                    self._ensure_tool(run_id, assistant, tool_data)

    def _ensure_tool(self, run_id: str, assistant: TranscriptNode, data: dict[str, object]) -> ToolTranscript | None:
        call_id = data.get("call_id")
        if not isinstance(call_id, str) or not call_id:
            return None
        key = (run_id, call_id)
        if key in self._seen_tool_calls:
            return self._tools_by_call.get(key)
        self._seen_tool_calls.add(key)
        if self.detail_level == "minimal":
            return None
        name = str(data.get("name") or data.get("tool") or "tool")
        if self.detail_level == "medium":
            assistant.add_node(Static(f"tool_call: {name}", markup=False, classes="transcript-tool-summary"))
            self._scroll_after_transcript_change()
            return None
        existing = self._tools_by_call.get(key)
        if existing is not None:
            return existing
        arguments = data.get("arguments", {})
        formatted = json.dumps(arguments, ensure_ascii=False, indent=2, default=str)
        argument_body = MarkdownBody(f"```json\n{formatted}\n```")
        result_body = MarkdownBody("")
        arguments_node = TranscriptNode("arguments", argument_body, collapsed=False)
        result_node = TranscriptNode("result", result_body, collapsed=False)
        status = StatusLeaf(str(data.get("status", "pending")))
        node = TranscriptNode(f"tool_call: {name}", arguments_node, result_node, status, collapsed=True)
        self._register_body(argument_body, assistant)
        self._register_body(result_body, assistant)
        self.transcript_nodes.append(node)
        self._node_top_level[node] = assistant
        assistant.add_node(node)
        tool = ToolTranscript(node, argument_body, result_body, status)
        self._tools_by_call[key] = tool
        node.set_activity(status.status in {"pending", "running"})
        self._scroll_after_transcript_change()
        return tool

    def _tool_for_event(
        self, run_id: str, assistant: TranscriptNode, message: str, data: dict[str, object]
    ) -> ToolTranscript | None:
        call_id = data.get("call_id")
        if isinstance(call_id, str) and call_id:
            key = (run_id, call_id)
            if key in self._seen_tool_calls:
                return self._tools_by_call.get(key)
            details = dict(data)
            details.setdefault("name", message or data.get("tool", "tool"))
            return self._ensure_tool(run_id, assistant, details)
        return None

    def _handle_tool_call(self, run_id: str, assistant: TranscriptNode, message: str, data: dict[str, object]) -> None:
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
            current[0].collapsed = True
        current = self._response_by_run.pop(run_id, None)
        if current is not None:
            current[0].set_activity(False)
        for (tool_run_id, _), tool in self._tools_by_call.items():
            if tool_run_id == run_id:
                tool.node.set_activity(False)
