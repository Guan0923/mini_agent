from __future__ import annotations

import asyncio

from backend.runtime.core.contracts import QuestionOption, UserQuestion
from tui.view import TerminalView
from tui.widgets import ChoiceItem


def test_review_panel_embeds_choices_and_preserves_input_draft() -> None:
    async def scenario() -> None:
        completed: list[tuple[str, str | None]] = []
        view = TerminalView()
        async with view.run_test() as pilot:
            view.begin_conversation("context remains visible")
            view.begin_review(
                "PLAN REVIEW",
                "Choose an action",
                "## Details\n\nReview this plan.",
                (ChoiceItem("continue", "Continue"), ChoiceItem("cancel", "Cancel")),
                lambda choice, supplement: completed.append((choice, supplement)),
            )
            await pilot.pause()

            assert view.choice_panel.display is True
            assert view.question_header.parent is view.choice_panel
            assert view.review_details.parent is view.choice_panel
            assert view.input.display is True
            assert view.focused is view.input
            assert view.transcript.display is True
            assert view.choice_menu.rows[0].has_class("-highlighted-choice")

            view.input.value = "keep this draft"
            await pilot.press("down", "enter")
            await pilot.pause()

            assert completed == [("cancel", None)]
            assert view.choice_panel.display is False
            assert view.input.value == "keep this draft"
            assert view.focused is view.input

    asyncio.run(scenario())


def test_review_panel_highlights_requested_initial_choice() -> None:
    async def scenario() -> None:
        view = TerminalView()
        async with view.run_test() as pilot:
            view.begin_review(
                "DISPLAY MODE",
                "Current: Medium",
                "Choose a display mode.",
                (
                    ChoiceItem("minimal", "Minimal"),
                    ChoiceItem("medium", "Medium"),
                    ChoiceItem("verbose", "Verbose"),
                ),
                lambda _choice, _supplement: None,
                initial_choice_id="medium",
            )
            await pilot.pause()

            assert view.choice_menu.index == 1
            assert view.choice_menu.rows[1].has_class("-highlighted-choice")

    asyncio.run(scenario())


def test_question_navigation_and_choice_highlight_do_not_wrap() -> None:
    async def scenario() -> None:
        questions = (
            UserQuestion(
                "first",
                "First",
                "Choose the first option.",
                (QuestionOption("One", "First option"), QuestionOption("Two", "Second option")),
            ),
            UserQuestion(
                "second",
                "Second",
                "Choose the second option.",
                (QuestionOption("Three", "Third option"), QuestionOption("Four", "Fourth option")),
            ),
        )
        view = TerminalView()
        async with view.run_test() as pilot:
            view.begin_questionnaire(questions, lambda _answers: None)
            await pilot.pause()

            first = view.question_menu
            assert view.question_index == 0
            await pilot.press("up")
            assert first.index == 0
            await pilot.press("down", "down", "down")
            assert first.index == len(first.rows) - 1
            await pilot.press("down")
            assert first.index == len(first.rows) - 1

            await pilot.press("right")
            assert view.question_index == 1
            await pilot.press("right")
            assert view.question_index == 1
            await pilot.press("left")
            assert view.question_index == 0
            await pilot.press("left")
            assert view.question_index == 0
            assert view.input.display is True
            assert view.focused is view.input

    asyncio.run(scenario())


def test_input_frame_focuses_input_and_tracks_explicit_newlines() -> None:
    async def scenario() -> None:
        view = TerminalView()
        async with view.run_test() as pilot:
            assert view.input.soft_wrap is False
            assert view.input_frame.has_class("-single-line")
            assert view.input.styles.height.value == 1

            view.set_focus(None)
            await pilot.click("#input-frame")
            assert view.focused is view.input

            view.input.value = "first"
            await pilot.press("ctrl+j")
            await pilot.pause()
            assert view.input.value == "first\n"
            assert view.input_frame.has_class("-multiline")
            assert view.input.styles.height.value == 2

            view.input.value = "first"
            await pilot.pause()
            assert view.input_frame.has_class("-single-line")
            assert view.input.styles.height.value == 1

    asyncio.run(scenario())
