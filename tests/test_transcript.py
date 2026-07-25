from __future__ import annotations

import asyncio

from backend.runtime.core.events import RuntimeEvent
from tui.rendering.mirror import TranscriptTextMirror
from tui.rendering.transcript import ProcessingProgress, TranscriptScroll
from tui.screens.history import HistoryScreen
from tui.view import TerminalView


def _event(kind: str, message: str = "", **data: object) -> RuntimeEvent:
    return RuntimeEvent(kind, message, {"run_id": "run_1", **data})


def test_text_mirror_tracks_sections_and_materializes_by_revision() -> None:
    mirror = TranscriptTextMirror()
    user = object()
    body = object()
    system = object()

    mirror.add_top_level(user, None)
    mirror.add_body(user, body, "hello")
    mirror.add_top_level(system, "SYSTEM")

    assert mirror.length == len("hello\nSYSTEM")
    assert mirror.materialization_count == 0
    assert mirror.text == "hello\nSYSTEM"
    assert mirror.text == "hello\nSYSTEM"
    assert mirror.materialization_count == 1

    assert mirror.update_body(body, "updated") is True
    assert mirror.materialization_count == 1
    assert mirror.text == "updated\nSYSTEM"
    assert mirror.materialization_count == 2

    mirror.remove_top_level(user)
    assert mirror.length == len("SYSTEM")
    assert mirror.text == "SYSTEM"


def test_transcript_scroll_reads_latest_mirror_for_selection() -> None:
    mirror = TranscriptTextMirror()
    owner = object()
    body = object()
    mirror.add_top_level(owner, None)
    mirror.add_body(owner, body, "first")
    transcript = TranscriptScroll()
    transcript.set_text_source(mirror.snapshot)

    assert transcript.text == "first"
    mirror.update_body(body, "first\nsecond")
    transcript.select_all()

    assert transcript.selected_text == "first\nsecond"


def test_metadata_events_do_not_schedule_transcript_reconcile() -> None:
    async def scenario() -> None:
        view = TerminalView()
        async with view.run_test() as pilot:
            view.begin_conversation("task")
            view.handle_runtime_event(_event("run_started"))
            await pilot.pause()
            assert view._reconcile_scheduled is False

            for kind in ("model_request", "model_response", "model_repair", "context_usage", "strategy"):
                view.handle_runtime_event(_event(kind, kind))
                assert view._reconcile_scheduled is False

    asyncio.run(scenario())


def test_many_tools_update_incrementally_until_text_is_read() -> None:
    async def scenario() -> None:
        view = TerminalView(detail_level="verbose")
        async with view.run_test() as pilot:
            view.begin_conversation("inspect files")
            view.handle_runtime_event(_event("run_started"))
            tools = [
                {
                    "name": "read_file",
                    "call_id": f"call_{index}",
                    "arguments": {"path": f"file_{index}.txt"},
                    "status": "pending",
                }
                for index in range(30)
            ]
            view.handle_runtime_event(
                _event(
                    "assistant_message",
                    exchange_id="exchange_1",
                    message={"content": None, "reasoning": None, "tool_messages": tools},
                )
            )
            for index in range(30):
                call_id = f"call_{index}"
                view.handle_runtime_event(
                    _event(
                        "tool_call",
                        "read_file",
                        call_id=call_id,
                        arguments={"path": f"file_{index}.txt"},
                    )
                )
                view.handle_runtime_event(
                    _event("tool_result", f"result-{index}-" + "x" * 1_000, call_id=call_id, tool="read_file")
                )
            await pilot.pause()

            mirror = view._transcript_mirror
            assert mirror.materialization_count == 0
            text = view.transcript_text
            assert "file_0.txt" in text
            assert "result-29-" in text
            assert mirror.materialization_count == 1
            assert view.transcript_text == text
            assert mirror.materialization_count == 1

            view.handle_runtime_event(_event("tool_result", "updated", call_id="call_0", tool="read_file"))
            await pilot.pause()
            assert mirror.materialization_count == 1
            assert "updated" in view.transcript_text
            assert mirror.materialization_count == 2

    asyncio.run(scenario())


def test_retention_and_copy_use_the_latest_mirror(monkeypatch) -> None:
    async def scenario() -> None:
        view = TerminalView(transcript_limit=20)
        copied: list[str] = []
        monkeypatch.setattr(view, "copy_to_clipboard", copied.append)
        async with view.run_test() as pilot:
            view.begin_conversation("first-message")
            view.handle_runtime_event(_event("run_started"))
            view.handle_runtime_event(_event("run_finished"))
            view.begin_conversation("second-message")
            await pilot.pause()

            assert view.transcript_text == "second-message"
            view.transcript.select_all()
            assert view.copy_transcript_selection() is True
            assert copied == ["second-message"]

    asyncio.run(scenario())


def test_history_clear_and_plain_append_keep_text_compatible() -> None:
    async def scenario() -> None:
        view = TerminalView()
        async with view.run_test() as pilot:
            view.load_history(
                [
                    {"role": "user", "content": "question"},
                    {"role": "tool", "content": "hidden"},
                    {"role": "assistant", "content": "answer"},
                ]
            )
            await pilot.pause()
            assert view.transcript_text == "question\nanswer"

            view.clear()
            await pilot.pause()
            assert view.transcript_text == ""

            view.write("plain", end="")
            view.flush_now()
            assert view.transcript_text == "plain"

    asyncio.run(scenario())


def test_queued_message_uses_its_own_panel_and_survives_history_screen() -> None:
    async def scenario() -> None:
        view = TerminalView()
        async with view.run_test() as pilot:
            view.queue_message("Use the first result only")
            await pilot.pause()

            assert view.transcript_text == ""
            assert view.queued_messages.messages == ["Use the first result only"]
            assert view.queued_messages.display is True

            view.show_history("session_1", [{"role": "user", "content": "completed task"}])
            await pilot.pause()
            assert isinstance(view.screen, HistoryScreen)

            await pilot.press("escape")
            await pilot.pause()
            assert view.queued_messages.messages == ["Use the first result only"]
            assert view.queued_messages.display is True

    asyncio.run(scenario())


def test_minimal_detail_hides_thinking_and_tools_but_shows_progress_and_response() -> None:
    async def scenario() -> None:
        view = TerminalView(detail_level="minimal")
        async with view.run_test() as pilot:
            view.begin_conversation("inspect files")
            view.handle_runtime_event(_event("run_started"))
            view.handle_runtime_event(_event("thinking_start"))
            view.handle_runtime_event(_event("thinking_delta", "private reasoning"))
            view.handle_runtime_event(
                _event(
                    "tool_call",
                    "read_file",
                    call_id="call_1",
                    arguments={"path": "secret.txt"},
                )
            )
            view.handle_runtime_event(_event("tool_result", "private result", call_id="call_1", tool="read_file"))
            view.handle_runtime_event(_event("response_start"))
            view.handle_runtime_event(_event("response_delta", "public answer"))
            view.handle_runtime_event(_event("response_end"))
            view.handle_runtime_event(_event("run_finished"))
            await pilot.pause()

            progress = next(iter(view.query(ProcessingProgress)))
            assert progress.running is False
            assert str(progress.render()) == "处理完成"
            assert view._thinking_by_run == {}
            assert view._tools_by_call == {}
            assert "public answer" in view.transcript_text
            assert "private reasoning" not in view.transcript_text
            assert "private result" not in view.transcript_text

    asyncio.run(scenario())


def test_medium_detail_shows_only_tool_name_and_keeps_response() -> None:
    async def scenario() -> None:
        view = TerminalView()
        assert view.detail_level == "medium"
        async with view.run_test() as pilot:
            view.begin_conversation("inspect files")
            view.handle_runtime_event(_event("run_started"))
            view.handle_runtime_event(_event("thinking_start"))
            view.handle_runtime_event(_event("thinking_delta", "private reasoning"))
            view.handle_runtime_event(
                _event(
                    "tool_call",
                    "read_file",
                    call_id="call_1",
                    arguments={"path": "secret.txt"},
                )
            )
            view.handle_runtime_event(_event("tool_result", "private result", call_id="call_1", tool="read_file"))
            view.handle_runtime_event(_event("response", "public answer"))
            await pilot.pause()

            summaries = list(view.query(".transcript-tool-summary"))
            assert len(summaries) == 1
            assert str(summaries[0].render()) == "tool_call: read_file"
            assert view._thinking_by_run == {}
            assert view._tools_by_call == {}
            assert "public answer" in view.transcript_text
            assert "private reasoning" not in view.transcript_text
            assert "private result" not in view.transcript_text

    asyncio.run(scenario())


def test_verbose_detail_shows_reasoning_and_keeps_tool_collapsed() -> None:
    async def scenario() -> None:
        view = TerminalView(detail_level="verbose")
        async with view.run_test() as pilot:
            view.begin_conversation("inspect files")
            view.handle_runtime_event(_event("run_started"))
            view.handle_runtime_event(_event("thinking_start"))
            view.handle_runtime_event(_event("thinking_delta", "reasoning"))
            view.handle_runtime_event(_event("thinking_end"))
            view.handle_runtime_event(
                _event(
                    "tool_call",
                    "read_file",
                    call_id="call_1",
                    arguments={"path": "file.txt"},
                )
            )
            view.handle_runtime_event(_event("tool_result", "result", call_id="call_1", tool="read_file"))
            view.handle_runtime_event(_event("response", "public answer"))
            await pilot.pause()

            assert any(node.title_text == "think_content" for node in view.transcript_nodes)
            tool = view._tools_by_call[("run_1", "call_1")]
            assert tool.node.collapsed is True
            assert "file.txt" in view.transcript_text
            assert "result" in view.transcript_text
            assert "public answer" in view.transcript_text
            assert not list(view.query(".transcript-tool-summary"))

    asyncio.run(scenario())
