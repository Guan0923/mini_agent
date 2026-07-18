from __future__ import annotations

import asyncio

from textual import events
from textual.color import Color
from textual.widgets import Label, Rule

from mini_agent.runtime.contracts import QuestionOption, UserQuestion
from mini_agent.runtime.user_input import OTHER_OPTION_LABEL, parse_user_input_questions
from mini_agent.tui.view import RUNNING_STATUS_WORDS, TerminalView


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

def three_questions() -> tuple[UserQuestion, ...]:
    return (
        *questions(),
        UserQuestion(
            "format",
            "Format",
            "Which output format should be used?",
            (
                QuestionOption("Markdown", "Use a Markdown document."),
                QuestionOption("Text", "Use plain text."),
            ),
        ),
    )



def test_textual_view_reserves_bottom_input_and_scrollable_transcript() -> None:
    async def scenario() -> None:
        view = TerminalView()
        async with view.run_test(size=(80, 20)):
            children = list(view.screen.children)
            status_bar = view.status_line.parent
            assert status_bar is view.context_progress.parent
            assert children[-2:] == [view.input, status_bar]
            assert list(view.query(Rule)) == []
            assert view.transcript.region.bottom == view.input.region.y
            assert view.input.region.bottom == status_bar.region.y
            assert view.status_line.region.right == view.context_progress.region.x
            assert view.status_line.region.y == view.context_progress.region.y
            assert view.input.styles.height.value == 3
            assert view.input.styles.margin.bottom == 0
            assert view.status_line.styles.height.value == 1
            assert view.context_progress.styles.height.value == 1
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


def test_multiline_input_keeps_three_rows_then_grows_to_four() -> None:
    async def scenario() -> None:
        view = TerminalView()
        async with view.run_test(size=(80, 20)) as pilot:
            for line_count in range(1, 5):
                view.input.value = "\n".join(str(index) for index in range(line_count))
                await pilot.pause()
                assert view.input.styles.height.value == max(3, line_count)

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
            assert view.question_menu.rows[-1].choice.label == OTHER_OPTION_LABEL

            await pilot.press("down", "enter")
            assert "2/2" in str(view.question_header.render())

            await pilot.press("up", "tab")
            assert view.questionnaire_custom_input is True
            row = view.question_menu.highlighted_row
            editor = row.editor
            assert editor.parent is row
            assert view.input.value == ""
            editor.value = "Only update storage code"
            await pilot.press("enter")

            assert completed == [
                {
                    "storage": ["JSONL"],
                    "scope": ["Only update storage code"],
                }
            ]
            assert view.questionnaire_active is False

    asyncio.run(scenario())


def test_questionnaire_replaces_exact_model_other_with_client_other() -> None:
    async def scenario() -> None:
        view = TerminalView()
        completed: list[dict[str, list[str]]] = []
        parsed = parse_user_input_questions(
            {
                "questions": [
                    {
                        "id": "details",
                        "header": "Details",
                        "question": "What should be used?",
                        "options": [{"label": " 其他 ", "description": "Provide a custom answer."}],
                    }
                ]
            }
        )
        async with view.run_test() as pilot:
            view.begin_questionnaire(parsed, completed.append)
            await pilot.pause()

            assert [row.choice.label for row in view.question_menu.rows] == [OTHER_OPTION_LABEL]
            await pilot.press("tab")
            editor = view.question_menu.highlighted_row.editor
            editor.value = "Custom value"
            await pilot.press("enter")

            assert completed == [{"details": ["Custom value"]}]

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
            assert view.focused is view.question_menu

    asyncio.run(scenario())

def test_questionnaire_switches_independent_lists_without_wrapping() -> None:
    async def scenario() -> None:
        view = TerminalView()
        async with view.run_test() as pilot:
            view.begin_questionnaire(three_questions(), lambda _answers: None)
            await pilot.pause()

            lists = view.question_lists
            lists[0].index = 1
            await pilot.press("right", "right", "right")

            assert view.question_index == 2
            assert [item.display for item in lists] == [False, False, True]

            await pilot.press("left", "left", "left")

            assert view.question_index == 0
            assert lists[0].index == 1
            assert [item.display for item in lists] == [True, False, False]

    asyncio.run(scenario())


def test_questionnaire_answers_next_unanswered_question_then_submits() -> None:
    async def scenario() -> None:
        view = TerminalView()
        completed: list[dict[str, list[str]]] = []
        async with view.run_test() as pilot:
            view.begin_questionnaire(three_questions(), completed.append)
            await pilot.pause()

            await pilot.press("right", "down", "enter")
            assert view.question_index == 2

            await pilot.press("enter")
            assert view.question_index == 0

            await pilot.press("enter")

            assert completed == [
                {
                    "storage": ["SQLite"],
                    "scope": ["Shared"],
                    "format": ["Markdown"],
                }
            ]
            assert view.questionnaire_active is False

    asyncio.run(scenario())


def test_inline_custom_input_keeps_arrow_keys_inside_editor() -> None:
    async def scenario() -> None:
        view = TerminalView()
        async with view.run_test() as pilot:
            view.begin_questionnaire(questions(), lambda _answers: None)
            await pilot.pause()

            await pilot.press("up", "tab")
            editor = view.question_menu.highlighted_row.editor
            editor.value = "Custom answer"
            editor.cursor_position = len(editor.value)

            await pilot.press("left")

            assert view.question_index == 0
            assert view.focused is editor
            assert editor.cursor_position == len(editor.value) - 1

            await pilot.press("escape", "right")
            assert view.question_index == 1

    asyncio.run(scenario())


def test_main_input_still_submits_while_questionnaire_is_active() -> None:
    async def scenario() -> None:
        view = TerminalView()
        async with view.run_test() as pilot:
            view.begin_questionnaire(questions()[:1], lambda _answers: None)
            await pilot.pause()
            view.input.value = "queued follow-up"
            view.input.focus()

            await pilot.press("enter")

            assert await asyncio.wait_for(view.submissions.get(), 1) == "queued follow-up"
            assert view.questionnaire_active is True

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
            agent_status = view._running_status
            view.transcript.load_text("copy")
            view.transcript.select_all()
            view.copy_transcript_selection()
            assert str(view.status_line.content) == " COPIED — 4 characters"

            await asyncio.sleep(1.6)
            await pilot.pause()
            assert str(view.status_line.content) == f" AGENT | {agent_status}"

            view.transcript.select_all()
            view.copy_transcript_selection()
            view.set_ui(status="PLAN | RUNNING")
            plan_status = view._running_status
            assert str(view.status_line.content) == f" PLAN | {plan_status}"
            await asyncio.sleep(1.6)
            await pilot.pause()

            assert str(view.status_line.content) == f" PLAN | {plan_status}"

    asyncio.run(scenario())


def test_running_status_catalog_contains_distinct_emoji_words() -> None:
    assert len(RUNNING_STATUS_WORDS) == 37
    assert "🏃 RUNNING" in RUNNING_STATUS_WORDS
    assert all(word.strip() and any(ord(char) > 127 for char in word) for word in RUNNING_STATUS_WORDS)


def test_running_status_rotates_on_random_timer_and_stops_when_idle() -> None:
    class FakeTimer:
        def __init__(self) -> None:
            self.stopped = False

        def stop(self) -> None:
            self.stopped = True

    class FakeRandom:
        def __init__(self) -> None:
            self.delays: list[tuple[float, float]] = []
            self.selected: list[str] = []

        def uniform(self, lower: float, upper: float) -> float:
            self.delays.append((lower, upper))
            return 7.0

        def choice(self, choices: tuple[str, ...]) -> str:
            selected = choices[0]
            self.selected.append(selected)
            return selected

    async def scenario() -> None:
        random_source = FakeRandom()
        view = TerminalView(status_random=random_source)
        scheduled: list[tuple[FakeTimer, float, object]] = []

        def fake_set_timer(delay: float, callback) -> FakeTimer:
            timer = FakeTimer()
            scheduled.append((timer, delay, callback))
            return timer

        async with view.run_test() as pilot:
            view.set_timer = fake_set_timer
            view.set_ui(
                status="AGENT | RUNNING | PERMISSION: FULL ACCESS",
                interrupt_enabled=True,
            )
            await pilot.pause()

            assert random_source.selected
            assert str(view.status_line.content) == (
                f" AGENT | {random_source.selected[0]} | PERMISSION: FULL ACCESS"
            )
            assert len(scheduled) == 1
            assert 5 <= scheduled[0][1] <= 10

            scheduled[0][2]()
            assert random_source.selected[1] != random_source.selected[0]
            assert str(view.status_line.content) == (
                f" AGENT | {random_source.selected[1]} | PERMISSION: FULL ACCESS"
            )
            assert len(scheduled) == 2
            assert 5 <= scheduled[1][1] <= 10

            view._copy_notice_timer = FakeTimer()
            view.status_line.update(" COPIED — 4 characters")
            scheduled[1][2]()
            assert random_source.selected[2] != random_source.selected[1]
            assert str(view.status_line.content) == " COPIED — 4 characters"

            view._copy_notice_timer = None
            view._render_status()
            assert str(view.status_line.content) == (
                f" AGENT | {random_source.selected[2]} | PERMISSION: FULL ACCESS"
            )
            view.set_ui(status="AGENT | IDLE | PERMISSION: FULL ACCESS")
            assert len(scheduled) == 3
            active_timer = scheduled[2][0]
            assert active_timer.stopped is True
            assert str(view.status_line.content) == " AGENT | IDLE | PERMISSION: FULL ACCESS"

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


def test_context_progress_renders_threshold_and_usage_states() -> None:
    async def scenario() -> None:
        view = TerminalView()
        async with view.run_test(size=(80, 20)) as pilot:
            unknown = view.context_progress.render()
            assert "CONTEXT N/A" in unknown.plain
            assert unknown.plain.count("┊") == 1

            rendered = {}
            for used in (790, 800, 1_000, 1_200):
                view.set_context_usage(used, 1_000, 0.8)
                await pilot.pause()
                rendered[used] = view.context_progress.render()

            assert "790 / 1,000 79%" in rendered[790].plain
            assert "800 / 1,000 80%" in rendered[800].plain
            assert "1,000 / 1,000 100%" in rendered[1_000].plain
            assert "1,200 / 1,000 120%" in rendered[1_200].plain
            assert len({value.plain.index("┊") for value in rendered.values()}) == 1
            assert any("#65b8a6" in str(span.style) for span in rendered[790].spans)
            assert any("#e3b65f" in str(span.style) for span in rendered[800].spans)
            assert any("#e26464" in str(span.style) for span in rendered[1_000].spans)
            assert any("#e26464" in str(span.style) for span in rendered[1_200].spans)

    asyncio.run(scenario())


def test_context_progress_keeps_marker_visible_in_narrow_layout() -> None:
    async def scenario() -> None:
        view = TerminalView()
        async with view.run_test(size=(16, 12)) as pilot:
            view.set_context_usage(800, 1_000, 0.8)
            await pilot.pause()

            rendered = view.context_progress.render()
            assert "┊" in rendered.plain
            assert len(rendered.plain) <= view.context_progress.size.width
            assert view.input.region.bottom == view.context_progress.region.y
            assert view.status_line.region.right == view.context_progress.region.x

    asyncio.run(scenario())
