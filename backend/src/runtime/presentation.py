"""Ordered presentation segments derived from a runtime event stream."""

from __future__ import annotations

from typing import Any

from .core.events import RuntimeEvent


class RunPresentationTracker:
    """Turn one run's transient events into stable, ordered UI segments.

    Segments are snapshots: every update contains the complete current text or
    tool list, so clients can replace a segment by ``segment_id`` safely even
    when SSE frames are retried or arrive out of order.
    """

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self._sequence = 0
        self._segments: dict[str, dict[str, Any]] = {}
        self._current_thinking: str | None = None
        self._current_response: str | None = None
        self._tool_batch: str | None = None

    def consume(self, event: RuntimeEvent) -> list[RuntimeEvent]:
        kind = event.kind
        data = event.data
        updates: list[dict[str, Any]] = []

        if kind == "thinking_start":
            self._close_streaming_response()
            updates.append(self._create("thinking", status="streaming", text=""))
            self._current_thinking = updates[-1]["segment_id"]
        elif kind == "thinking_delta":
            segment = self._ensure_thinking()
            segment["text"] = f"{segment.get('text', '')}{event.message}"
            segment["status"] = "streaming"
            updates.append(segment)
        elif kind == "thinking_end":
            if self._current_thinking:
                updates.append(self._update(self._current_thinking, status="completed"))
        elif kind == "response_start":
            self._close_streaming_thinking()
            updates.append(self._create("response", status="streaming", text="", final=False))
            self._current_response = updates[-1]["segment_id"]
        elif kind == "response_delta":
            segment = self._ensure_response()
            segment["text"] = f"{segment.get('text', '')}{event.message}"
            segment["status"] = "streaming"
            updates.append(segment)
        elif kind == "response_end":
            if self._current_response:
                updates.append(self._update(self._current_response, status="completed"))
        elif kind == "assistant_message":
            updates.extend(self._assistant_message(data))
        elif kind == "tool_call":
            updates.append(self._tool_call(event))
        elif kind in {"tool_result", "tool_failed", "tool_indeterminate"}:
            update = self._tool_result(event)
            if update is not None:
                updates.append(update)
        elif kind in {"cancelled", "error", "run_finished", "run_terminated", "run_interrupted"}:
            final_answer = data.get("final_answer")
            if kind == "run_finished" and isinstance(final_answer, str) and final_answer:
                if self._current_response and self._segments[self._current_response].get("text") == final_answer:
                    segment = self._segments[self._current_response]
                else:
                    segment = self._ensure_response()
                segment["text"] = final_answer
                segment["status"] = "completed"
                segment["final"] = True
                updates.append(segment)
            updates.extend(self._close_terminal(f"{event.message or kind}"))

        return [self._event(segment, event) for segment in updates]

    def _new_id(self) -> str:
        self._sequence += 1
        return f"{self.run_id}:segment:{self._sequence}"

    def _create(self, segment_type: str, *, status: str, **values: Any) -> dict[str, Any]:
        segment = {
            "sequence": self._sequence + 1,
            "segment_id": self._new_id(),
            "segment_type": segment_type,
            "status": status,
            **values,
        }
        if segment_type == "tool_batch":
            segment.setdefault("tools", [])
        self._segments[segment["segment_id"]] = segment
        return segment

    def _update(self, segment_id: str, **values: Any) -> dict[str, Any]:
        segment = self._segments[segment_id]
        segment.update(values)
        return segment

    def _event(self, segment: dict[str, Any], source: RuntimeEvent) -> RuntimeEvent:
        return RuntimeEvent("run_segment", str(segment["segment_type"]), dict(segment), timestamp=source.timestamp)

    def _ensure_thinking(self) -> dict[str, Any]:
        if self._current_thinking and self._segments[self._current_thinking]["status"] == "streaming":
            return self._segments[self._current_thinking]
        segment = self._create("thinking", status="streaming", text="")
        self._current_thinking = segment["segment_id"]
        return segment

    def _ensure_response(self) -> dict[str, Any]:
        if self._current_response and self._segments[self._current_response]["status"] == "streaming":
            return self._segments[self._current_response]
        segment = self._create("response", status="streaming", text="", final=False)
        self._current_response = segment["segment_id"]
        return segment

    def _close_streaming_thinking(self) -> None:
        if self._current_thinking:
            segment = self._segments[self._current_thinking]
            if segment["status"] == "streaming":
                segment["status"] = "completed"

    def _close_streaming_response(self) -> None:
        if self._current_response:
            segment = self._segments[self._current_response]
            if segment["status"] == "streaming":
                segment["status"] = "completed"

    def _assistant_message(self, data: dict[str, Any]) -> list[dict[str, Any]]:
        message = data.get("message")
        if not isinstance(message, dict):
            return []
        updates: list[dict[str, Any]] = []
        reasoning = message.get("reasoning")
        if isinstance(reasoning, str) and reasoning:
            segment = (
                self._segments[self._current_thinking]
                if self._current_thinking and self._segments[self._current_thinking].get("text") == reasoning
                else self._ensure_thinking()
            )
            segment["text"] = reasoning
            segment["status"] = "completed"
            updates.append(segment)
            self._current_thinking = segment["segment_id"]
        content = message.get("content")
        tools = message.get("tool_messages")
        if isinstance(content, str) and content:
            if self._current_response and self._segments[self._current_response].get("text") == content:
                segment = self._segments[self._current_response]
            elif self._current_response and self._segments[self._current_response]["status"] == "streaming":
                segment = self._segments[self._current_response]
                segment["text"] = content
            else:
                segment = self._create("response", status="completed", text=content, final=not bool(tools))
                self._current_response = segment["segment_id"]
            segment["status"] = "completed"
            segment["final"] = not bool(tools)
            updates.append(segment)
        if isinstance(tools, list) and tools:
            batch = self._ensure_tool_batch()
            for tool in tools:
                if isinstance(tool, dict):
                    self._merge_tool(batch, tool, default_status=str(tool.get("status") or "pending"))
            updates.append(batch)
        return updates

    def _ensure_tool_batch(self) -> dict[str, Any]:
        if self._tool_batch:
            batch = self._segments[self._tool_batch]
            if batch["status"] == "streaming":
                return batch
        self._close_streaming_response()
        batch = self._create("tool_batch", status="streaming", tools=[])
        self._tool_batch = batch["segment_id"]
        return batch

    def _merge_tool(self, batch: dict[str, Any], raw: dict[str, Any], *, default_status: str) -> dict[str, Any]:
        call_id = str(raw.get("call_id") or raw.get("tool_call_id") or "")
        if not call_id:
            return batch
        tools = batch.setdefault("tools", [])
        existing = next((item for item in tools if item.get("call_id") == call_id), None)
        status = default_status if default_status in {"pending", "succeeded", "failed"} else "pending"
        item = existing or {
            "call_id": call_id,
            "name": str(raw.get("name") or raw.get("tool") or "工具"),
            "arguments": dict(raw.get("arguments") or {}),
            "status": status,
        }
        item.update(
            {
                "name": str(raw.get("name") or raw.get("tool") or item.get("name") or "工具"),
                "arguments": dict(raw.get("arguments") or item.get("arguments") or {}),
                "status": status,
            }
        )
        if raw.get("content") is not None or raw.get("result") is not None:
            item["result"] = str(raw.get("content") if raw.get("content") is not None else raw.get("result"))
        if raw.get("error") is not None:
            item["error"] = str(raw["error"])
        if raw.get("failure_code") is not None:
            item["failure_code"] = str(raw["failure_code"])
        if existing is None:
            tools.append(item)
        return batch

    def _tool_call(self, event: RuntimeEvent) -> dict[str, Any]:
        batch = self._ensure_tool_batch()
        self._merge_tool(
            batch,
            {
                "call_id": event.data.get("call_id"),
                "name": event.data.get("tool") or event.message,
                "arguments": event.data.get("arguments"),
            },
            default_status="pending",
        )
        return batch

    def _tool_result(self, event: RuntimeEvent) -> dict[str, Any] | None:
        call_id = str(event.data.get("call_id") or "")
        if not call_id:
            return None
        batch = self._ensure_tool_batch()
        failed = event.kind in {"tool_failed", "tool_indeterminate"}
        raw = {
            "call_id": call_id,
            "name": event.data.get("tool") or event.message,
            "arguments": event.data.get("arguments") or {},
            "status": "failed" if failed else "succeeded",
            "result": event.data.get("result") or (event.message if not failed else None),
            "error": event.data.get("error") or (event.message if failed else None),
            "failure_code": event.data.get("failure_code"),
        }
        self._merge_tool(batch, raw, default_status="failed" if failed else "succeeded")
        tools = batch.get("tools", [])
        if tools and all(item.get("status") in {"succeeded", "failed"} for item in tools):
            batch["status"] = "failed" if any(item.get("status") == "failed" for item in tools) else "completed"
        return batch

    def _close_terminal(self, error: str) -> list[dict[str, Any]]:
        updates: list[dict[str, Any]] = []
        for segment in self._segments.values():
            if segment.get("status") != "streaming":
                continue
            if segment.get("segment_type") == "tool_batch":
                for tool in segment.get("tools", []):
                    if tool.get("status") == "pending":
                        tool["status"] = "failed"
                        tool["error"] = error
                segment["status"] = (
                    "failed" if any(t.get("status") == "failed" for t in segment.get("tools", [])) else "completed"
                )
            else:
                segment["status"] = "failed" if error else "completed"
            updates.append(segment)
        return updates
