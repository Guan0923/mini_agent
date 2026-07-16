from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document

from mini_agent.tui.completion import SlashCommandCompleter


def _completions(text: str):
    completer = SlashCommandCompleter()
    return list(completer.get_completions(Document(text), CompleteEvent(completion_requested=True)))


def test_completes_plan_from_a_prefix() -> None:
    completions = _completions("/p")

    assert [item.text for item in completions] == ["/plan", "/permission"]
    assert completions[0].start_position == -2


def test_completes_permission_from_a_prefix() -> None:
    completions = _completions("/per")

    assert [item.text for item in completions] == ["/permission"]
    assert completions[0].start_position == -4


def test_completes_multiple_commands_from_a_prefix() -> None:
    assert [item.text for item in _completions("/s")] == ["/sessions", "/session"]


def test_completes_clear_from_a_prefix() -> None:
    assert [item.text for item in _completions("/c")] == ["/clear"]


def test_completes_a_command_after_task_text() -> None:
    completions = _completions("summarize README.md /h")

    assert "/history" in [item.text for item in completions]
    assert next(item for item in completions if item.text == "/history").start_position == -2


def test_does_not_complete_slashes_inside_paths_or_urls() -> None:
    assert _completions("docs/architecture.md") == []
    assert _completions("https://example.com") == []


def test_does_not_complete_legacy_slash_arguments() -> None:
    assert _completions("/new/学习记录") == []


def test_completion_replaces_only_the_current_command_prefix() -> None:
    completion = _completions("summarize README.md /p")[0]

    assert completion.text == "/plan"
    assert completion.start_position == -2
