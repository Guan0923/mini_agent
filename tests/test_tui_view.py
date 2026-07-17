from __future__ import annotations

import asyncio

from textual import events
from textual.color import Color
from textual.widgets import Label, Rule

from mini_agent.runtime.contracts import QuestionOption, UserQuestion
from mini_agent.runtime.user_input import OTHER_OPTION_LABEL
from mini_agent.tui.view import TerminalView


def questions() -> tuple[UserQuestion, ...]:
    return (
        UserQuestion(
            "storage",
            "Storage",
            "Where should the result be stored?",
            (
                QuestionOption("SQLite", "Use the existing database."),
                QuestionOption("JSONL", "Use the existing audit stream."),
            ),
        ),
        UserQuestion(
            "scope",
            "Scope",
            "How broad should the change be?",
            (
                QuestionOption("Focused", "Change only the requested workflow."),
                QuestionOption("Shared", "Update the shared runtime behavior."),
            ),
        ),
    )


def test_textual_view_reserves_bottom_input_and_scrollable_transcript() -> None:
    async def scenario() -> None:
        view = TerminalView()
        async with view.run_test(size=(80, 20)):
            children = list(view.screen.children)
            assert children[-2:] == [view.input, view.status_line]
            assert list(view.query(Rule)) == []
            assert view.transcript.region.bottom == view.input.region.y
            assert view.input.region.bottom == view.status_line.region.y
            assert view.input.styles.height.value == 1
            assert view.input.styles.margin.bottom == 0
            assert view.status_line.styles.height.value == 1
            assert view.input.styles.background == Color.parse("#171c21")
            assert view.status_line.styles.background == Color.parse("#263442")
            assert view.transcript.soft_wrap is True
            assert view.transcript.read_only is True
            assert view.transcript.styles.overflow_y == "scroll"
            assert list(view.query(Label)) == []

    asyncio.run(scenario())


def test_stop_finishes_running_textual_app() -> None:
    async def scenario() -> None:
        view = TerminalView()
        view_task = asyncio.create_task(view.run_async(headless=True, size=(80, 20)))
        await asyncio.wait_for(view._mounted_event.wait(), 1)
        await asyncio.sleep(0)

        view.stop()

        assert view._closed is False
        await asyncio.wait_for(view_task, 1)

    asyncio.run(scenario())


def test_stop_is_idempotent_and_ignores_late_writes() -> None:
    async def scenario() -> None:
        view = TerminalView()
        view_task = asyncio.create_task(view.run_async(headless=True, size=(80, 20)))
        await asyncio.wait_for(view._mounted_event.wait(), 1)
        view.write("kept", "")

        view.stop()
        view.stop()
        view.write("ignored", "")

        await asyncio.wait_for(view_task, 1)
        assert view.transcript_text == "kept"

    asyncio.run(scenario())


def test_fast_transcript_updates_are_batched_without_touching_input() -> None:
    async def scenario() -> None:
        view = TerminalView()
        async with view.run_test() as pilot:
            view.input.value = "draft message"
            for index in range(100):
                view.write(str(index), "")
            await asyncio.sleep(0.06)
            await pilot.pause()

            assert view.transcript_text == "".join(str(index) for index in range(100))
            assert view.input.value == "draft message"

    asyncio.run(scenario())


def test_transcript_limit_and_smart_follow() -> None:
    async def scenario() -> None:
        view = TerminalView(transcript_limit=100)
        async with view.run_test(size=(40, 10)) as pilot:
            view.write("\n".join(f"line {index}" for index in range(40)))
            view.flush_now()
            await pilot.pause()

            assert len(view.transcript_text) == 100
            assert view.transcript_text.startswith("[Earlier terminal output omitted]\n")
            assert view.follow_tail is True

            view.scroll_page_up()
            assert view.follow_tail is False
            old_scroll = view.transcript.scroll_y

            view.write("new output")
            view.flush_now()
            await pilot.pause()

            assert view.follow_tail is False
            assert view.transcript.scroll_y == old_scroll

            view.follow_latest()
            await pilot.pause()
            assert view.follow_tail is True
            assert view.transcript.scroll_y == view.transcript.max_scroll_y

    asyncio.run(scenario())


def test_input_acceptance_uses_submission_queue() -> None:
    async def scenario() -> None:
        view = TerminalView()
        async with view.run_test() as pilot:
            view.input.value = "hello"
            view.input.focus()
            await pilot.press("enter")

            assert await view.submissions.get() == "hello"
            assert view.input.value == ""

    asyncio.run(scenario())


def test_ctrl_j_inserts_newline_at_cursor_without_submitting() -> None:
    async def scenario() -> None:
        view = TerminalView()
        async with view.run_test() as pilot:
            view.input.value = "firstsecond"
            view.input.cursor_position = 5
            view.input.focus()

            await pilot.press("ctrl+j")

            assert view.input.value == "first\nsecond"
            assert view.input.cursor_position == 6
            assert view.submissions.empty()

    asyncio.run(scenario())


def test_enter_submits_complete_multiline_input() -> None:
    async def scenario() -> None:
        view = TerminalView()
        async with view.run_test() as pilot:
            view.input.value = "first\nsecond"
            view.input.focus()

            await pilot.press("enter")

            assert await asyncio.wait_for(view.submissions.get(), 1) == "first\nsecond"
            assert view.input.value == ""

    asyncio.run(scenario())


def test_multiline_input_grows_to_four_rows_then_keeps_fixed_height() -> None:
    async def scenario() -> None:
        view = TerminalView()
        async with view.run_test(size=(80, 20)) as pilot:
            for line_count in range(1, 5):
                view.input.value = "\n".join(str(index) for index in range(line_count))
                await pilot.pause()
                assert view.input.styles.height.value == line_count

            view.input.value = "\n".join(str(index) for index in range(5))
            await pilot.pause()

            assert view.input.styles.height.value == 4
            assert view.input.virtual_size.height >= 5

    asyncio.run(scenario())


def test_soft_wrapped_input_grows_to_four_rows() -> None:
    async def scenario() -> None:
        view = TerminalView()
        async with view.run_test(size=(20, 12)) as pilot:
            view.input.value = "wrapped content " * 8
            await pilot.pause()

            assert view.input.styles.height.value == 4
            assert view.input.wrapped_document.height > 4

    asyncio.run(scenario())


def test_control_c_submits_quit_command() -> None:
    async def scenario() -> None:
        view = TerminalView()
        async with view.run_test() as pilot:
            view.input.value = "unfinished draft"
            view.input.focus()
            await pilot.press("ctrl+c")

            assert await asyncio.wait_for(view.submissions.get(), 1) == "/quit"

    asyncio.run(scenario())


def test_escape_requests_active_run_interrupt_and_preserves_input() -> None:
    async def scenario() -> None:
        view = TerminalView()
        async with view.run_test() as pilot:
            view.set_ui(status="AGENT | RUNNING", interrupt_enabled=True)
            view.input.value = "unfinished draft"
            view.input.focus()

            await pilot.press("escape")

            await asyncio.wait_for(view.interrupts.get(), 1)
            assert view.input.value == "unfinished draft"

    asyncio.run(scenario())


def test_idle_escape_closes_completion_without_requesting_interrupt() -> None:
    async def scenario() -> None:
        view = TerminalView()
        async with view.run_test() as pilot:
            view.set_ui(status="AGENT | IDLE", interrupt_enabled=False)
            view.input.focus()
            await pilot.press("/", "p")
            assert view.completion_menu.display is True

            await pilot.press("escape")

            assert view.completion_menu.display is False
            assert view.interrupts.empty()

    asyncio.run(scenario())


def test_command_completion_menu_accepts_selected_candidate() -> None:
    async def scenario() -> None:
        view = TerminalView()
        async with view.run_test() as pilot:
            view.input.focus()
            await pilot.press("/", "p")
            await pilot.pause()

            assert view.completion_menu.display is True
            assert [option.prompt for option in view.completion_menu._options] == [
                "/plan — Enter read-only planning and discussion mode.",
                "/permission — Choose the in-memory tool approval mode.",
            ]

            await pilot.press("down", "tab")
            assert view.input.value == "/permission"
            assert view.completion_menu.display is False

    asyncio.run(scenario())


def test_questionnaire_collects_selected_and_custom_answers() -> None:
    async def scenario() -> None:
        view = TerminalView()
        completed: list[dict[str, list[str]]] = []
        async with view.run_test() as pilot:
            view.begin_questionnaire(questions(), completed.append)
            await pilot.pause()

            assert view.questionnaire_active is True
            assert [str(option.prompt) for option in view.question_menu._options][-1] == OTHER_OPTION_LABEL

            await pilot.press("down", "enter")
            assert "2/2" in str(view.question_header.render())

            await pilot.press("up", "tab")
            assert view.questionnaire_custom_input is True
            view.input.value = "Only update storage code"
            await pilot.press("enter")

            assert completed == [
                {
                    "storage": ["JSONL"],
                    "scope": ["Only update storage code"],
                }
            ]
            assert view.questionnaire_active is False

    asyncio.run(scenario())


def test_questionnaire_escape_returns_from_custom_input_to_options() -> None:
    async def scenario() -> None:
        view = TerminalView()
        completed: list[dict[str, list[str]]] = []
        async with view.run_test() as pilot:
            view.begin_questionnaire(questions()[:1], completed.append)
            await pilot.pause()

            await pilot.press("up", "tab")
            assert view.questionnaire_custom_input is True
            await pilot.press("escape")
            assert view.questionnaire_custom_input is False

            await pilot.press("down", "enter")
            assert completed == [{"storage": ["SQLite"]}]

    asyncio.run(scenario())


def test_active_questionnaire_escape_requests_run_interrupt() -> None:
    async def scenario() -> None:
        view = TerminalView()
        completed: list[dict[str, list[str]]] = []
        async with view.run_test() as pilot:
            view.begin_questionnaire(questions()[:1], completed.append)
            view.set_ui(status="PLAN QUESTIONS | Select answers", interrupt_enabled=True)
            await pilot.pause()

            await pilot.press("escape")

            await asyncio.wait_for(view.interrupts.get(), 1)
            assert completed == []

    asyncio.run(scenario())


def test_questionnaire_tab_on_regular_option_keeps_choice_mode() -> None:
    async def scenario() -> None:
        view = TerminalView()
        async with view.run_test() as pilot:
            view.begin_questionnaire(questions()[:1], lambda _answers: None)
            await pilot.pause()

            await pilot.press("tab")

            assert view.questionnaire_active is True
            assert view.questionnaire_custom_input is False
            assert view.focused is view.input

    asyncio.run(scenario())


def test_cancelling_inactive_questionnaire_preserves_input() -> None:
    async def scenario() -> None:
        view = TerminalView()
        async with view.run_test():
            view.input.value = "draft"

            view.cancel_questionnaire()

            assert view.input.value == "draft"

    asyncio.run(scenario())


def test_right_click_copies_selection_clears_it_and_shows_feedback(monkeypatch) -> None:
    async def scenario() -> None:
        view = TerminalView()
        copied: list[str] = []
        monkeypatch.setattr(view, "copy_to_clipboard", copied.append)
        async with view.run_test() as pilot:
            view.transcript.load_text("copy this")
            view.transcript.select_all()
            selection_end = view.transcript.selection.end

            await pilot.click("#transcript", button=3)
            await pilot.pause()

            assert copied == ["copy this"]
            assert view.transcript.selection.is_empty
            assert view.transcript.selection.end == selection_end
            assert view.focused is view.input
            assert str(view.status_line.content) == " COPIED — 9 characters"

    asyncio.run(scenario())


def test_input_paste_inserts_text_without_copying_transcript_selection(monkeypatch) -> None:
    async def scenario() -> None:
        view = TerminalView()
        copied: list[str] = []
        monkeypatch.setattr(view, "copy_to_clipboard", copied.append)
        async with view.run_test() as pilot:
            view.input.value = "draft"
            view.input.cursor_position = len(view.input.value)
            view.transcript.load_text("selected output")
            view.transcript.select_all()
            transcript_selection = view.transcript.selection

            view.input.post_message(events.Paste(" pasted"))
            await pilot.pause()

            assert copied == []
            assert view.input.value == "draft pasted"
            assert view.transcript.selection == transcript_selection
            assert str(view.status_line.content) == " AGENT | IDLE"

    asyncio.run(scenario())


def test_copy_notice_restores_latest_status_without_stale_timer_overwrite(monkeypatch) -> None:
    async def scenario() -> None:
        view = TerminalView()
        monkeypatch.setattr(view, "copy_to_clipboard", lambda _text: None)
        async with view.run_test() as pilot:
            view.set_ui(status="AGENT | RUNNING")
            view.transcript.load_text("copy")
            view.transcript.select_all()
            view.copy_transcript_selection()
            assert str(view.status_line.content) == " COPIED — 4 characters"

            await asyncio.sleep(1.6)
            await pilot.pause()
            assert str(view.status_line.content) == " AGENT | RUNNING"

            view.transcript.select_all()
            view.copy_transcript_selection()
            view.set_ui(status="PLAN | RUNNING")
            assert str(view.status_line.content) == " PLAN | RUNNING"
            await asyncio.sleep(1.6)
            await pilot.pause()

            assert str(view.status_line.content) == " PLAN | RUNNING"

    asyncio.run(scenario())


def test_copy_without_selection_leaves_clipboard_and_status_unchanged(monkeypatch) -> None:
    async def scenario() -> None:
        view = TerminalView()
        copied: list[str] = []
        monkeypatch.setattr(view, "copy_to_clipboard", copied.append)
        async with view.run_test() as pilot:
            view.set_ui(status="AGENT | IDLE")
            view.transcript.load_text("not selected")

            await pilot.click("#transcript", button=3)
            await pilot.pause()

            assert copied == []
            assert str(view.status_line.content) == " AGENT | IDLE"

    asyncio.run(scenario())
