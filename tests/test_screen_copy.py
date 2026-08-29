from __future__ import annotations

import asyncio

from textual.app import App
from textual.widgets import Markdown

from tui.screens.history import HistoryScreen
from tui.screens.inspection import SessionsScreen, TraceScreen


async def _copy_all(screen, selector: str) -> tuple[str, list[str]]:
    app = App[None]()
    copied: list[str] = []
    app.copy_to_clipboard = copied.append  # type: ignore[method-assign]
    async with app.run_test() as pilot:
        app.push_screen(screen)
        await pilot.pause()
        log = screen.query_one(selector)
        if isinstance(screen, HistoryScreen):
            assert list(screen.query(Markdown))
        screen._select_all_in_widget(log)
        await pilot.pause()
        selected = screen.get_selected_text()
        assert selected

        await pilot.click(selector, button=3)
        await pilot.pause()

        assert screen.get_selected_text() is None
        return selected, copied


def test_history_content_is_selectable_and_right_click_copies() -> None:
    async def scenario() -> None:
        screen = HistoryScreen(
            "session-1",
            [
                {"role": "user", "content": "hello **world**"},
                {"role": "assistant", "content": "answer"},
            ],
        )

        selected, copied = await _copy_all(screen, "#history-log")

        assert "USER" in selected
        assert "hello world" in selected
        assert "ASSISTANT" in selected
        assert copied == [selected]

    asyncio.run(scenario())


def test_sessions_and_trace_preserve_text_when_copied() -> None:
    async def scenario() -> None:
        sessions = SessionsScreen(["session-one", "session-two"])
        selected, copied = await _copy_all(sessions, "#inspection-log")
        assert selected == "session-one\nsession-two"
        assert copied == [selected]

        trace_text = '{\n  "status": "completed",\n  "count": 2\n}'
        trace = TraceScreen("run-1", trace_text)
        selected, copied = await _copy_all(trace, "#inspection-log")
        assert selected == trace_text
        assert copied == [trace_text]

    asyncio.run(scenario())


def test_right_click_without_selection_does_not_copy() -> None:
    async def scenario() -> None:
        app = App[None]()
        copied: list[str] = []
        app.copy_to_clipboard = copied.append  # type: ignore[method-assign]
        screen = SessionsScreen(["session-one"])
        async with app.run_test() as pilot:
            app.push_screen(screen)
            await pilot.pause()

            await pilot.click("#inspection-log", button=3)
            await pilot.pause()

            assert copied == []

    asyncio.run(scenario())


def test_history_supports_real_mouse_drag_selection() -> None:
    async def scenario() -> None:
        app = App[None]()
        screen = HistoryScreen("session-1", [{"role": "user", "content": "selectable text here"}])
        async with app.run_test(size=(80, 24)) as pilot:
            app.push_screen(screen)
            await pilot.pause()

            await pilot.mouse_down("#history-log", offset=(1, 1))
            await pilot.hover("#history-log", offset=(15, 2))
            await pilot.mouse_up("#history-log", offset=(15, 2))
            await pilot.pause()

            assert screen.get_selected_text() == "selectable text here"

    asyncio.run(scenario())
