from pathlib import Path

import pytest

from mini_agent.tools import ToolError, WorkspaceFiles
from mini_agent.tui.references import FileReferenceExpander


def test_file_reference_expander_inlines_workspace_file_and_preserves_email(tmp_path: Path) -> None:
    (tmp_path / "notes.md").write_text("alpha\nbeta", encoding="utf-8")
    expander = FileReferenceExpander(WorkspaceFiles(tmp_path))

    result = expander.expand("Contact user@example.com and summarize @notes.md.")

    assert "user@example.com" in result
    assert "[Referenced file: notes.md]" in result
    assert "alpha\nbeta" in result
    assert "[End referenced file: notes.md]" in result


def test_file_reference_expander_rejects_files_outside_workspace(tmp_path: Path) -> None:
    expander = FileReferenceExpander(WorkspaceFiles(tmp_path))

    with pytest.raises(ToolError, match="workspace"):
        expander.expand("summarize @../outside.txt")


def test_file_reference_expander_enforces_file_size_limit(tmp_path: Path) -> None:
    (tmp_path / "large.txt").write_text("12345", encoding="utf-8")
    expander = FileReferenceExpander(WorkspaceFiles(tmp_path), max_file_chars=4)

    with pytest.raises(ToolError, match="Referenced file is too large"):
        expander.expand("summarize @large.txt")
