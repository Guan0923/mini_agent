from mini_agent.runtime.core.contracts import InterruptRequest
from mini_agent.tui.approval import TerminalApproval
from mini_agent.tui.interactive_approval import InteractiveApproval
from mini_agent.tui.tool_review import format_tool_review


def request(tool: str, arguments: dict) -> InterruptRequest:
    return InterruptRequest("tool", f"Call tool {tool}?", {"tool": tool, "arguments": arguments})


def test_builtin_tool_reviews_show_only_the_required_target_information() -> None:
    cases = [
        ("run_command", {"command": "python -m pytest", "timeout_seconds": 30}, "Run command", "python -m pytest"),
        ("write_file", {"path": "note.md", "content": "private body"}, "Create file", "note.md"),
        (
            "write_file",
            {"path": "note.md", "content": "private body", "overwrite": True},
            "Create or overwrite file",
            "note.md",
        ),
        (
            "edit_file",
            {"path": "note.md", "old_text": "private old", "new_text": "private new"},
            "Edit file",
            "note.md",
        ),
        (
            "web_search",
            {"query": "Python documentation", "max_results": 5},
            "Search the web",
            "Python documentation",
        ),
        (
            "web_fetch",
            {"url": "https://example.com", "max_chars": 1000},
            "Fetch web content",
            "https://example.com",
        ),
    ]

    for tool, arguments, action, target in cases:
        summary = format_tool_review(request(tool, arguments))

        assert summary.action == action
        assert target in summary.plain()
        assert "{" not in summary.plain()
        assert "private body" not in summary.plain()
        assert "private old" not in summary.plain()
        assert "private new" not in summary.plain()


def test_multiline_command_is_preserved_and_markdown_values_are_safe() -> None:
    fallback = format_tool_review(
        request("run_command_*", {"script_[name]": "first line\nsecond line", "nested": {"x": 1}})
    )
    command = format_tool_review(request("run_command", {"command": "echo first\necho second"}))

    assert "run\\_command\\_\\*" in fallback.markdown()
    assert "script_[name]: <multiline text, 22 characters>" in fallback.plain()
    assert "nested: <object with 1 fields>" in fallback.plain()
    assert "echo first\necho second" in command.plain()
    assert "    echo first\n    echo second" in command.markdown()


def test_terminal_and_textual_reviews_share_the_same_summary(capsys) -> None:
    tool_request = request(
        "write_file",
        {"path": "docs/note.md", "content": "must stay hidden", "overwrite": True},
    )
    summary = format_tool_review(tool_request)

    TerminalApproval().render_request(tool_request)

    assert summary.plain() in capsys.readouterr().out
    assert InteractiveApproval._tool_details(tool_request) == summary.markdown()
    assert "must stay hidden" not in summary.plain()
    assert "must stay hidden" not in summary.markdown()


def test_unknown_tool_fallback_has_no_raw_json_and_handles_missing_arguments() -> None:
    fallback = format_tool_review(request("deploy", {"region": "cn-north", "options": [1, 2]})).plain()
    missing = format_tool_review(InterruptRequest("tool", "Call tool?", {"tool": "deploy"})).plain()

    assert fallback == (
        "Tool: deploy\n"
        "Action: Call tool\n"
        "Parameters:\n"
        "options: <list with 2 items>\n"
        "region: cn-north"
    )
    assert "{" not in fallback and '"' not in fallback
    assert missing == "Tool: deploy\nAction: Call tool"
