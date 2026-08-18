from backend.runtime.core.events import RuntimeEvent
from backend.runtime.presentation import RunPresentationTracker


def _events(*events: RuntimeEvent):
    tracker = RunPresentationTracker("run_test")
    return [item for event in events for item in tracker.consume(event)]


def test_multi_round_presentation_keeps_distinct_segments_and_tool_batch():
    events = _events(
        RuntimeEvent("thinking_start"),
        RuntimeEvent("thinking_delta", "先分析"),
        RuntimeEvent("thinking_end"),
        RuntimeEvent("response_delta", "我准备调用工具"),
        RuntimeEvent("tool_call", "glob", {"call_id": "a", "arguments": {"pattern": "*.py"}}),
        RuntimeEvent("tool_call", "run_command", {"call_id": "b", "arguments": {"command": "pytest"}}),
        RuntimeEvent("tool_result", "files", {"call_id": "a", "tool": "glob", "result": "files"}),
        RuntimeEvent("tool_result", "tests", {"call_id": "b", "tool": "run_command", "result": "tests"}),
        RuntimeEvent("thinking_start"),
        RuntimeEvent("thinking_delta", "整理结果"),
        RuntimeEvent("thinking_end"),
        RuntimeEvent("assistant_message", data={"message": {"content": "最终说明", "tool_messages": []}}),
    )
    snapshots = {event.data["segment_id"]: event.data for event in events}
    ordered = sorted(snapshots.values(), key=lambda item: item["sequence"])
    assert [item["segment_type"] for item in ordered] == ["thinking", "response", "tool_batch", "thinking", "response"]
    batch = ordered[2]
    assert [tool["call_id"] for tool in batch["tools"]] == ["a", "b"]
    assert all(tool["status"] == "succeeded" for tool in batch["tools"])


def test_terminal_event_fails_pending_tools():
    events = _events(
        RuntimeEvent("tool_call", "glob", {"call_id": "a", "arguments": {}}),
        RuntimeEvent("cancelled", "用户取消"),
    )
    latest = events[-1].data
    assert latest["segment_type"] == "tool_batch"
    assert latest["status"] == "failed"
    assert latest["tools"][0]["status"] == "failed"
    assert latest["tools"][0]["error"] == "用户取消"


def test_assistant_boundary_does_not_duplicate_streamed_response():
    events = _events(
        RuntimeEvent("response_start"),
        RuntimeEvent("response_delta", "准备调用工具"),
        RuntimeEvent("response_end"),
        RuntimeEvent("assistant_message", data={"message": {"content": "准备调用工具", "tool_messages": [{"call_id": "a", "name": "glob", "arguments": {}}]}}),
    )
    latest_by_id = {event.data["segment_id"]: event.data for event in events}
    assert [item["segment_type"] for item in sorted(latest_by_id.values(), key=lambda item: item["sequence"])] == ["response", "tool_batch"]


def test_run_finished_reuses_final_response_segment():
    events = _events(
        RuntimeEvent("assistant_message", data={"message": {"content": "完成", "tool_messages": []}}),
        RuntimeEvent("run_finished", "completed", {"final_answer": "完成"}),
    )
    latest_by_id = {event.data["segment_id"]: event.data for event in events}
    assert len(latest_by_id) == 1
    assert next(iter(latest_by_id.values()))["final"] is True
