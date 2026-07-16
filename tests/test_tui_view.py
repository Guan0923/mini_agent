from __future__ import annotations

import asyncio

from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from mini_agent.tui.view import TerminalView


def test_full_screen_view_reserves_one_bottom_input_row() -> None:
    async def scenario() -> None:
        view = TerminalView(asyncio.get_running_loop(), output=DummyOutput())

        assert view.application.full_screen is True
        assert view.input.window.height.preferred == 1
        assert view.input.window.height.min == 1
        assert view.input.window.height.max == 1
        assert view.status_window.height == 1

    asyncio.run(scenario())


def test_fast_transcript_updates_are_batched_without_touching_input(monkeypatch) -> None:
    async def scenario() -> None:
        view = TerminalView(asyncio.get_running_loop(), output=DummyOutput())
        view.input.buffer.text = "draft message"
        view.input.buffer.cursor_position = 5
        append_calls = 0
        original_append = view._append_transcript

        def record_append(value: str) -> None:
            nonlocal append_calls
            append_calls += 1
            original_append(value)

        monkeypatch.setattr(view, "_append_transcript", record_append)
        await asyncio.to_thread(lambda: [view.write(str(index), "") for index in range(100)])

        assert view.transcript_text == ""
        await asyncio.sleep(0.06)

        assert append_calls == 1
        assert view.transcript_text == "".join(str(index) for index in range(100))
        assert view.input.buffer.text == "draft message"
        assert view.input.buffer.cursor_position == 5

    asyncio.run(scenario())


def test_transcript_limit_and_smart_follow() -> None:
    async def scenario() -> None:
        view = TerminalView(asyncio.get_running_loop(), transcript_limit=100, output=DummyOutput())
        view.write("\n".join(f"line {index}" for index in range(40)))
        await asyncio.sleep(0.06)

        assert len(view.transcript_text) == 100
        assert view.transcript_text.startswith("[Earlier terminal output omitted]\n")
        assert view.follow_tail is True

        view.scroll_page_up()
        paused_cursor = view.transcript.buffer.cursor_position
        assert view.follow_tail is False

        view.write("new output")
        await asyncio.sleep(0.06)

        assert view.follow_tail is False
        assert view.transcript.buffer.cursor_position <= paused_cursor

        view.follow_latest()
        assert view.follow_tail is True
        assert view.transcript.buffer.cursor_position == len(view.transcript_text)

    asyncio.run(scenario())


def test_input_acceptance_uses_submission_queue() -> None:
    async def scenario() -> None:
        view = TerminalView(asyncio.get_running_loop(), output=DummyOutput())
        view.input.buffer.text = "hello"

        view.input.buffer.validate_and_handle()

        assert await view.submissions.get() == "hello"
        assert view.input.buffer.text == ""

    asyncio.run(scenario())


def test_full_screen_application_accepts_pipe_input_and_exits() -> None:
    async def scenario() -> None:
        with create_pipe_input() as pipe_input:
            view = TerminalView(
                asyncio.get_running_loop(),
                input=pipe_input,
                output=DummyOutput(),
            )
            application = asyncio.create_task(view.run_async())
            await asyncio.sleep(0.02)

            pipe_input.send_text("hello\r")

            assert await asyncio.wait_for(view.submissions.get(), 1) == "hello"
            view.stop()
            await application

    asyncio.run(scenario())
