"""Tests for workspace-confined file discovery, search, and mutation."""

from pathlib import Path

import pytest

from backend.tools import ConfirmationRequired, ToolError, WorkspaceFiles, build_tool_registry
from backend.tools.filesystem import io as filesystem_io


def test_read_file_returns_bounded_lf_line_range(tmp_path: Path) -> None:
    (tmp_path / "note.txt").write_bytes(b"one\r\ntwo\r\nthree\r\n")

    result = WorkspaceFiles(tmp_path).read_file("note.txt", start_line=2, max_lines=2)

    assert result == "note.txt: lines 2-3 of 3\ntwo\nthree\n"


def test_read_file_validates_ranges_paths_and_utf8(tmp_path: Path) -> None:
    files = WorkspaceFiles(tmp_path)
    (tmp_path / "binary.dat").write_bytes(b"\xff")

    with pytest.raises(ToolError, match="start_line"):
        files.read_file("binary.dat", start_line=0)
    with pytest.raises(ToolError, match="max_lines"):
        files.read_file("binary.dat", max_lines=1_001)
    with pytest.raises(ToolError, match="workspace"):
        files.read_file("../outside.txt")
    with pytest.raises(ToolError, match="valid UTF-8"):
        files.read_file("binary.dat")


def test_glob_matches_root_and_nested_files_and_skips_internal_directories(tmp_path: Path) -> None:
    (tmp_path / "root.py").write_text("root", encoding="utf-8")
    (tmp_path / "src" / "nested").mkdir(parents=True)
    (tmp_path / "src" / "direct.py").write_text("direct", encoding="utf-8")
    (tmp_path / "src" / "nested" / "deep.py").write_text("deep", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "ignored.py").write_text("ignored", encoding="utf-8")

    result = WorkspaceFiles(tmp_path).glob("**/*.py")

    assert result.splitlines() == ["root.py", "src/direct.py", "src/nested/deep.py"]


def test_glob_is_case_sensitive_and_reports_truncation(tmp_path: Path) -> None:
    (tmp_path / "A.py").write_text("A", encoding="utf-8")
    (tmp_path / "b.py").write_text("b", encoding="utf-8")

    files = WorkspaceFiles(tmp_path)
    assert files.glob("*.PY") == "(no matches)"
    assert files.glob("*.py", max_results=1).splitlines() == [
        "A.py",
        "... results truncated at 1 files.",
    ]


def test_grep_supports_literal_regex_case_and_glob_filters(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("Alpha\nbeta 42\n", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("alpha\nbeta 7\n", encoding="utf-8")
    files = WorkspaceFiles(tmp_path)

    assert files.grep("alpha", case_sensitive=False).splitlines() == [
        "app.py:1:Alpha",
        "notes.txt:1:alpha",
    ]
    assert files.grep(r"beta \d+", glob="**/*.py", regex=True) == "app.py:2:beta 42"
    with pytest.raises(ToolError, match="Invalid regular expression"):
        files.grep("[", regex=True)


def test_grep_skips_binary_non_utf8_and_oversized_files(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "match.txt").write_text("needle", encoding="utf-8")
    (tmp_path / "binary.dat").write_bytes(b"needle\x00")
    (tmp_path / "invalid.txt").write_bytes(b"needle\xff")
    (tmp_path / "large.txt").write_text("needle in a large file", encoding="utf-8")
    monkeypatch.setattr(WorkspaceFiles, "_MAX_SEARCH_FILE_BYTES", 10)

    result = WorkspaceFiles(tmp_path).grep("needle")

    assert result.splitlines() == [
        "match.txt:1:needle",
        "Skipped 3 binary, non-UTF-8, or oversized files.",
    ]


def test_write_file_creates_and_requires_explicit_overwrite(tmp_path: Path) -> None:
    files = WorkspaceFiles(tmp_path)

    assert files.write_file("note.txt", "first") == "Created note.txt with 5 characters."
    with pytest.raises(ToolError, match="already exists"):
        files.write_file("note.txt", "second")
    assert (tmp_path / "note.txt").read_text(encoding="utf-8") == "first"

    assert files.write_file("note.txt", "second", overwrite=True) == "Replaced note.txt with 6 characters."
    assert (tmp_path / "note.txt").read_text(encoding="utf-8") == "second"
    assert files.write_file("missing/note.txt", "content") == "Created missing/note.txt with 7 characters."
    assert (tmp_path / "missing" / "note.txt").read_text(encoding="utf-8") == "content"


def test_create_directory_is_recursive_idempotent_and_registered_as_approved_write(tmp_path: Path) -> None:
    files = WorkspaceFiles(tmp_path)

    assert files.create_directory("one/two/three") == "Created directory one/two/three."
    assert files.create_directory("one/two/three") == "Directory already exists: one/two/three."
    assert (tmp_path / "one" / "two" / "three").is_dir()

    registry = build_tool_registry(tmp_path)
    assert "create_directory" in registry.names()
    assert "create_directory" not in registry.read_only_names()
    assert registry.requires_confirmation("create_directory") is True
    with pytest.raises(ConfirmationRequired):
        registry.invoke("create_directory", {"path": "approved"})
    with pytest.raises(ToolError, match="Invalid arguments"):
        registry.invoke("create_directory", {}, confirmed=True)


def test_create_directory_rejects_files_traversal_absolute_paths_and_links(tmp_path: Path) -> None:
    files = WorkspaceFiles(tmp_path)
    (tmp_path / "blocked").write_text("file", encoding="utf-8")

    with pytest.raises(ToolError, match="Not a directory"):
        files.create_directory("blocked/child")
    with pytest.raises(ToolError, match="workspace"):
        files.create_directory("../outside")
    with pytest.raises(ToolError, match="relative"):
        files.create_directory(str(tmp_path / "absolute"))

    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("Creating symbolic links is not permitted on this system.")
    with pytest.raises(ToolError, match="Symbolic links"):
        files.create_directory("link/child")


def test_write_file_keeps_created_parents_when_file_creation_fails(tmp_path: Path, monkeypatch) -> None:
    files = WorkspaceFiles(tmp_path)
    registry = build_tool_registry(tmp_path, workspace_files=files)

    def fail_create(_path: Path, _content: str) -> None:
        raise OSError("simulated file creation failure")

    monkeypatch.setattr(files, "_exclusive_create", fail_create)
    with pytest.raises(ToolError, match="simulated file creation failure"):
        registry.invoke(
            "write_file",
            {"path": "created/before/failure.txt", "content": "content"},
            confirmed=True,
        )

    assert (tmp_path / "created" / "before").is_dir()
    assert not (tmp_path / "created" / "before" / "failure.txt").exists()


def test_edit_file_replaces_one_match_and_preserves_crlf(tmp_path: Path) -> None:
    path = tmp_path / "note.txt"
    path.write_bytes(b"one\r\ntwo\r\n")

    result = WorkspaceFiles(tmp_path).edit_file("note.txt", "one\ntwo", "one\nchanged")

    assert result == "Edited note.txt: replaced 1 occurrence."
    assert path.read_bytes() == b"one\r\nchanged\r\n"


def test_edit_file_rejects_missing_or_ambiguous_matches_without_changes(tmp_path: Path) -> None:
    path = tmp_path / "note.txt"
    path.write_text("same\nsame\n", encoding="utf-8")
    files = WorkspaceFiles(tmp_path)

    with pytest.raises(ToolError, match="not found"):
        files.edit_file("note.txt", "missing", "new")
    with pytest.raises(ToolError, match="matched 2 locations"):
        files.edit_file("note.txt", "same", "new")
    with pytest.raises(ToolError, match="non-empty"):
        files.edit_file("note.txt", "", "new")
    assert path.read_text(encoding="utf-8") == "same\nsame\n"


def test_atomic_replace_failure_preserves_original_and_cleans_temporary_file(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "note.txt"
    path.write_text("original", encoding="utf-8")

    def fail_replace(_source, _destination) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr(filesystem_io.os, "replace", fail_replace)
    with pytest.raises(ToolError, match="replace failed"):
        WorkspaceFiles(tmp_path).write_file("note.txt", "updated", overwrite=True)

    assert path.read_text(encoding="utf-8") == "original"
    assert not list(tmp_path.glob(".note.txt.*.tmp"))


def test_write_operations_reject_symbolic_links(tmp_path: Path) -> None:
    target = tmp_path / "target.txt"
    target.write_text("target", encoding="utf-8")
    link = tmp_path / "link.txt"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("Creating symbolic links is not permitted on this system.")

    with pytest.raises(ToolError, match="Symbolic links"):
        WorkspaceFiles(tmp_path).write_file("link.txt", "changed", overwrite=True)
    assert target.read_text(encoding="utf-8") == "target"


def test_glob_stops_walking_once_the_directory_bound_is_reached(tmp_path: Path) -> None:
    class BoundedFiles(WorkspaceFiles):
        _MAX_WALKED_FILES = 3

    for index in range(10):
        (tmp_path / f"file{index}.txt").write_text(str(index), encoding="utf-8")

    result = BoundedFiles(tmp_path).glob("**/*.txt")

    assert "directory search truncated" in result
    assert "file9.txt" not in result
