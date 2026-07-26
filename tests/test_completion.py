import asyncio

from tui.components.completion import SlashCommandCompleter
from tui.view import TerminalView


def _completions(text: str):
    return SlashCommandCompleter().suggestions(text, len(text))


def test_completes_plan_from_a_prefix() -> None:
    completions = _completions("/p")

    assert [item.value for item in completions] == ["/plan", "/permission"]
    assert completions[0].start_position == 0


def test_completes_permission_from_a_prefix() -> None:
    completions = _completions("/per")

    assert [item.value for item in completions] == ["/permission"]
    assert completions[0].start_position == 0


def test_completes_multiple_commands_from_a_prefix() -> None:
    assert [item.value for item in _completions("/s")] == ["/sessions", "/skills"]


def test_resume_completion_replaces_use_and_session() -> None:
    assert [item.value for item in _completions("/r")] == ["/resume"]


def test_completes_clear_from_a_prefix() -> None:
    assert [item.value for item in _completions("/c")] == ["/clear", "/compact"]


def test_completes_a_command_after_task_text() -> None:
    completions = _completions("summarize README.md /h")

    assert "/history" in [item.value for item in completions]
    assert next(item for item in completions if item.value == "/history").start_position == 20


def test_does_not_complete_slashes_inside_paths_or_urls() -> None:
    assert _completions("docs/architecture.md") == []
    assert _completions("https://example.com") == []


def test_does_not_complete_legacy_slash_arguments() -> None:
    assert _completions("/new/学习记录") == []


def test_completion_replaces_only_the_current_command_prefix() -> None:
    completion = _completions("summarize README.md /p")[0]

    assert completion.value == "/plan"
    assert completion.start_position == 20


def test_enter_submits_terminal_input() -> None:
    async def scenario() -> None:
        view = TerminalView()
        async with view.run_test() as pilot:
            view.input.value = "hello"
            view.input.focus()

            await pilot.press("enter")

            assert await asyncio.wait_for(view.submissions.get(), 1) == "hello"
            assert view.input.value == ""

    asyncio.run(scenario())
