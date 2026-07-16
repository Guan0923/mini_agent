from __future__ import annotations

import asyncio

from textual import events
from textual.widgets import Label

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
            assert view.input.styles.height.value == 1
            assert view.input.styles.margin.bottom == 1
            assert view.transcript.soft_wrap is True
            assert view.transcript.read_only is True
            assert view.transcript.styles.overflow_y == "scroll"
            assert list(view.query(Label)) == []

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
            await asyncio.sleep(0.06)
            await pilot.pause()

            assert len(view.transcript_text) == 100
            assert view.transcript_text.startswith("[Earlier terminal output omitted]\n")
            assert view.follow_tail is True

            view.scroll_page_up()
            assert view.follow_tail is False
            old_scroll = view.transcript.scroll_y

            view.write("new output")
            await asyncio.sleep(0.06)
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


def test_control_c_submits_quit_command() -> None:
    async def scenario() -> None:
        view = TerminalView()
        async with view.run_test() as pilot:
            view.input.value = "unfinished draft"
            view.input.focus()
            await pilot.press("ctrl+c")

            assert await asyncio.wait_for(view.submissions.get(), 1) == "/quit"

    asyncio.run(scenario())


def test_command_completion_menu_accepts_selected_candidate() -> None:
    async def scenario() -> None:
        view = TerminalView()
        async with view.run_test() as pilot:
            view.input.focus()
            await pilot.press("/", "p")
            await pilot.pause()

            assert view.completion_menu.display is True
            assert [option.prompt for option in view.completion_menu._options] == ["/plan", "/permission"]

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


def test_right_click_copies_selection_and_restores_input_focus(monkeypatch) -> None:
    async def scenario() -> None:
        view = TerminalView()
        copied: list[str] = []
        monkeypatch.setattr(view, "copy_to_clipboard", copied.append)
        async with view.run_test() as pilot:
            view.transcript.load_text("copy this")
            view.transcript.select_all()
            await pilot.click("#transcript", button=3)
            await pilot.pause()

            assert copied == ["copy this"]
            assert view.focused is view.input

    asyncio.run(scenario())


def test_terminal_right_click_paste_is_consumed_as_copy_when_transcript_is_selected(monkeypatch) -> None:
    async def scenario() -> None:
        view = TerminalView()
        copied: list[str] = []
        monkeypatch.setattr(view, "copy_to_clipboard", copied.append)
        async with view.run_test() as pilot:
            view.input.value = "draft"
            view.transcript.load_text("selected output")
            view.transcript.select_all()

            view.input.post_message(events.Paste("old clipboard contents"))
            await pilot.pause()

            assert copied == ["selected output"]
            assert view.input.value == "draft"

    asyncio.run(scenario())
