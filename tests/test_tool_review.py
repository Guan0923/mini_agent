from backend.runtime.core.contracts import InterruptRequest
from tui.components.tool_review import format_tool_review


def test_tool_review_shows_only_known_tool_and_target() -> None:
    review = format_tool_review(
        InterruptRequest("tool", "", {"tool": "edit_file", "arguments": {"path": "src/main.py", "new_text": "x"}})
    )

    assert review.plain() == "Tool: edit_file\nTarget: src/main.py"
    assert review.markdown() == "**Tool:** edit\\_file\n\n**Target:** src/main\\.py"


def test_tool_review_maps_each_builtin_target() -> None:
    targets = {
        "run_command": ("command", "pytest -q"),
        "write_file": ("path", "notes.txt"),
        "web_search": ("query", "Python docs"),
        "web_fetch": ("url", "https://example.com/docs"),
    }

    for tool, (field, value) in targets.items():
        review = format_tool_review(InterruptRequest("tool", "", {"tool": tool, "arguments": {field: value}}))
        assert review.target == value


def test_tool_review_shortens_multiline_target() -> None:
    command = "first line\n" + "x" * 130

    review = format_tool_review(
        InterruptRequest("tool", "", {"tool": "run_command", "arguments": {"command": command}})
    )

    assert review.target == f"first line {'x' * 106}..."
    assert len(review.target) == 120


def test_tool_review_omits_unknown_tool_arguments() -> None:
    review = format_tool_review(
        InterruptRequest("tool", "", {"tool": "custom_tool", "arguments": {"secret": "do not show"}})
    )

    assert review.plain() == "Tool: custom_tool"
    assert "secret" not in review.markdown()
