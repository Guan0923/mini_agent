"""Tests for workspace-confined file discovery, search, and mutation."""

from pathlib import Path

import pytest

from backend.tools import ConfirmationRequired, ToolError, WorkspaceFiles, build_tool_registry
from backend.tools.filesystem import io as filesystem_io


def test_read_file_returns_bounded_lf_line_range(tmp_path: Path) -> None:
    path = tmp_path / "note.txt"
    path.write_bytes(b"one\r\ntwo\r\nthree\r\n")

    result = WorkspaceFiles(tmp_path).read_file(str(path), start_line=2, max_lines=2)

    assert result == "workspace:note.txt: lines 2-3 of 3\n2 | two\n3 | three\n"


def test_read_file_validates_ranges_paths_and_utf8(tmp_path: Path) -> None:
    files = WorkspaceFiles(tmp_path)
    (tmp_path / "binary.dat").write_bytes(b"\xff")

    with pytest.raises(ToolError, match="start_line"):
        files.read_file(str(tmp_path / "binary.dat"), start_line=0)
    with pytest.raises(ToolError, match="max_lines"):
        files.read_file(str(tmp_path / "binary.dat"), max_lines=1_001)
    with pytest.raises(ToolError, match="approved workspace"):
        files.read_file("../outside.txt")
    with pytest.raises(ToolError, match="valid UTF-8"):
        files.read_file(str(tmp_path / "binary.dat"))


def test_read_file_allows_only_absolute_paths_in_read_only_whitelist(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    skill_root = tmp_path / "user" / ".mini_agent" / "skills"
    nested = skill_root / "brainstorming" / "references" / "guide.md"
    outside = tmp_path / "outside.txt"
    workspace.mkdir()
    nested.parent.mkdir(parents=True)
    nested.write_text("skill guide", encoding="utf-8")
    outside.write_text("secret", encoding="utf-8")
    files = WorkspaceFiles(workspace, read_file_roots=(skill_root,))

    result = files.read_file(str(nested))

    assert result.endswith("\n1 | skill guide")
    assert nested.as_posix() in result
    with pytest.raises(ToolError, match="approved workspace"):
        files.read_file(str(outside))
    with pytest.raises(ToolError, match="approved workspace"):
        files.glob("*.md", path=str(skill_root))
    with pytest.raises(ToolError, match="approved workspace"):
        files.grep("guide", path=str(skill_root))
    with pytest.raises(ToolError, match="approved workspace"):
        files.write_file(str(nested), "changed")


def test_read_file_rejects_tilde_even_inside_configured_skill_root(tmp_path: Path, monkeypatch) -> None:
    profile = tmp_path / "profile"
    workspace = tmp_path / "workspace"
    skill_root = profile / ".mini_agent" / "skills"
    manifest = skill_root / "demo" / "SKILL.md"
    workspace.mkdir()
    manifest.parent.mkdir(parents=True)
    manifest.write_text("demo instructions", encoding="utf-8")
    monkeypatch.setenv("USERPROFILE", str(profile))
    monkeypatch.setenv("HOME", str(profile))
    files = WorkspaceFiles(workspace, read_file_roots=(skill_root,))

    with pytest.raises(ToolError, match="Not a file"):
        files.read_file("~/.mini_agent/skills/demo/SKILL.md")


def test_glob_matches_root_and_nested_files_and_skips_internal_directories(tmp_path: Path) -> None:
    (tmp_path / "root.py").write_text("root", encoding="utf-8")
    (tmp_path / "src" / "nested").mkdir(parents=True)
    (tmp_path / "src" / "direct.py").write_text("direct", encoding="utf-8")
    (tmp_path / "src" / "nested" / "deep.py").write_text("deep", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "ignored.py").write_text("ignored", encoding="utf-8")

    result = WorkspaceFiles(tmp_path).glob("**/*.py")

    assert result.splitlines() == [
        "workspace:root.py",
        "workspace:src/direct.py",
        "workspace:src/nested/deep.py",
    ]


def test_glob_is_case_sensitive_and_reports_truncation(tmp_path: Path) -> None:
    (tmp_path / "A.py").write_text("A", encoding="utf-8")
    (tmp_path / "b.py").write_text("b", encoding="utf-8")

    files = WorkspaceFiles(tmp_path)
    assert files.glob("*.PY") == "(no matches)"
    assert files.glob("*.py", max_results=1).splitlines() == [
        "workspace:A.py",
        "... results truncated at 1 files.",
    ]


def test_grep_supports_literal_regex_case_and_glob_filters(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("Alpha\nbeta 42\n", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("alpha\nbeta 7\n", encoding="utf-8")
    files = WorkspaceFiles(tmp_path)

    assert files.grep("alpha", case_sensitive=False).splitlines() == [
        "workspace:app.py:1:Alpha",
        "workspace:notes.txt:1:alpha",
    ]
    assert files.grep(r"beta \d+", glob="**/*.py", regex=True) == ("workspace:app.py:2:beta 42")
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
        "workspace:match.txt:1:needle",
        "Skipped 3 binary, non-UTF-8, or oversized files.",
    ]


def test_omitted_search_path_merges_both_workspaces_with_scoped_paths(tmp_path: Path) -> None:
    session_workspace = tmp_path / "session"
    project_workspace = tmp_path / "project"
    session_workspace.mkdir()
    project_workspace.mkdir()
    session_file = session_workspace / "same.txt"
    project_file = project_workspace / "same.txt"
    session_file.write_text("needle session", encoding="utf-8")
    project_file.write_text("needle project", encoding="utf-8")
    files = WorkspaceFiles(session_workspace, project_workspace=project_workspace)

    expected_paths = ["project:same.txt", "workspace:same.txt"]
    assert files.glob("*.txt").splitlines() == expected_paths
    assert files.grep("needle").splitlines() == sorted(
        (
            "workspace:same.txt:1:needle session",
            "project:same.txt:1:needle project",
        )
    )
    assert files.glob("*.txt", path=str(project_workspace)).splitlines() == ["project:same.txt"]


def test_read_file_numbers_empty_lines_and_supports_column_continuation(tmp_path: Path) -> None:
    path = tmp_path / "note.txt"
    path.write_text("alpha\n\nomega", encoding="utf-8", newline="")
    files = WorkspaceFiles(tmp_path)

    assert files.read_file(str(path), start_line=1, max_lines=3) == (
        "workspace:note.txt: lines 1-3 of 3\n1 | alpha\n2 | \n3 | omega"
    )
    assert files.read_file(str(path), start_line=1, max_lines=1, start_column=3) == (
        "workspace:note.txt: lines 1-1 of 3, starting at column 3\n1 | pha\n"
    )


def test_write_file_creates_and_requires_explicit_overwrite(tmp_path: Path) -> None:
    files = WorkspaceFiles(tmp_path)

    path = tmp_path / "note.txt"
    assert files.write_file(str(path), "first") == "Created workspace:note.txt with 5 characters."
    with pytest.raises(ToolError, match="already exists"):
        files.write_file(str(path), "second")
    assert (tmp_path / "note.txt").read_text(encoding="utf-8") == "first"

    assert files.write_file(str(path), "second", overwrite=True) == ("Replaced workspace:note.txt with 6 characters.")
    assert (tmp_path / "note.txt").read_text(encoding="utf-8") == "second"
    nested = tmp_path / "missing" / "note.txt"
    assert files.write_file(str(nested), "content") == "Created workspace:missing/note.txt with 7 characters."
    assert (tmp_path / "missing" / "note.txt").read_text(encoding="utf-8") == "content"


def test_create_directory_is_recursive_idempotent_and_registered_as_approved_write(tmp_path: Path) -> None:
    files = WorkspaceFiles(tmp_path)

    directory = tmp_path / "one" / "two" / "three"
    assert files.create_directory(str(directory)) == "Created directory workspace:one/two/three."
    assert files.create_directory(str(directory)) == "Directory already exists: workspace:one/two/three."
    assert (tmp_path / "one" / "two" / "three").is_dir()

    registry = build_tool_registry(tmp_path)
    assert "create_directory" in registry.names()
    assert "create_directory" not in registry.read_only_names()
    assert registry.requires_confirmation("create_directory") is True
    assert {name for name in registry.names() if registry.is_workspace_confined(name)} == {
        "create_directory",
        "write_file",
        "edit_file",
        "run_command",
    }
    with pytest.raises(ConfirmationRequired):
        registry.invoke("create_directory", {"path": str(tmp_path / "approved")})
    with pytest.raises(ToolError, match="Invalid arguments"):
        registry.invoke("create_directory", {}, confirmed=True)


def test_create_directory_rejects_files_traversal_absolute_paths_and_links(tmp_path: Path) -> None:
    files = WorkspaceFiles(tmp_path)
    (tmp_path / "blocked").write_text("file", encoding="utf-8")

    with pytest.raises(ToolError, match="Not a directory|Unable to inspect file path"):
        files.create_directory(str(tmp_path / "blocked" / "child"))
    with pytest.raises(ToolError, match="approved workspace"):
        files.create_directory("../outside")
    with pytest.raises(ToolError, match="approved workspace"):
        files.create_directory(str(tmp_path.parent / "absolute"))

    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("Creating symbolic links is not permitted on this system.")
    with pytest.raises(ToolError, match="Symbolic links"):
        files.create_directory(str(link / "child"))


def test_write_file_keeps_created_parents_when_file_creation_fails(tmp_path: Path, monkeypatch) -> None:
    files = WorkspaceFiles(tmp_path)
    registry = build_tool_registry(tmp_path, workspace_files=files)

    def fail_create(_path: Path, _content: str) -> None:
        raise OSError("simulated file creation failure")

    monkeypatch.setattr(files, "_exclusive_create", fail_create)
    with pytest.raises(ToolError, match="simulated file creation failure"):
        registry.invoke(
            "write_file",
            {"path": str(tmp_path / "created" / "before" / "failure.txt"), "content": "content"},
            confirmed=True,
        )

    assert (tmp_path / "created" / "before").is_dir()
    assert not (tmp_path / "created" / "before" / "failure.txt").exists()


def test_edit_file_replaces_one_match_and_preserves_crlf(tmp_path: Path) -> None:
    path = tmp_path / "note.txt"
    path.write_bytes(b"one\r\ntwo\r\n")

    result = WorkspaceFiles(tmp_path).edit_file(str(path), 1, 2, ["one", "two"], ["one", "changed"])

    assert result == "Edited workspace:note.txt: replaced lines 1-2."
    assert path.read_bytes() == b"one\r\nchanged\r\n"


def test_edit_file_rejects_stale_or_invalid_ranges_without_changes(tmp_path: Path) -> None:
    path = tmp_path / "note.txt"
    path.write_text("same\nsame\n", encoding="utf-8")
    files = WorkspaceFiles(tmp_path)

    with pytest.raises(ToolError, match="no longer match"):
        files.edit_file(str(path), 1, 1, ["missing"], ["new"])
    with pytest.raises(ToolError, match="greater than or equal"):
        files.edit_file(str(path), 2, 1, [], [])
    with pytest.raises(ToolError, match="length"):
        files.edit_file(str(path), 1, 2, ["same"], ["new"])
    with pytest.raises(ToolError, match="line-break"):
        files.edit_file(str(path), 1, 1, ["same"], ["new\nline"])
    assert path.read_text(encoding="utf-8") == "same\nsame\n"


def test_edit_file_distinguishes_deletion_from_one_blank_line(tmp_path: Path) -> None:
    delete_path = tmp_path / "delete.txt"
    blank_path = tmp_path / "blank.txt"
    delete_path.write_bytes(b"one\r\ntwo\r\nthree")
    blank_path.write_bytes(b"one\r\ntwo")
    files = WorkspaceFiles(tmp_path)

    files.edit_file(str(delete_path), 2, 2, ["two"], [])
    files.edit_file(str(blank_path), 2, 2, ["two"], [""])

    assert delete_path.read_bytes() == b"one\r\nthree"
    assert blank_path.read_bytes() == b"one\r\n\r\n"


def test_edit_file_rejects_a_concurrent_change_before_atomic_replace(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = tmp_path / "concurrent.txt"
    path.write_text("before\n", encoding="utf-8")
    files = WorkspaceFiles(tmp_path)
    atomic_replace = files._atomic_replace

    def race(file_path: Path, content: str, *, expected_content: str) -> None:
        file_path.write_text("concurrent\n", encoding="utf-8")
        atomic_replace(file_path, content, expected_content=expected_content)

    monkeypatch.setattr(files, "_atomic_replace", race)

    with pytest.raises(ToolError, match="changed during"):
        files.edit_file(str(path), 1, 1, ["before"], ["after"])
    assert path.read_text(encoding="utf-8") == "concurrent\n"


def test_atomic_replace_failure_preserves_original_and_cleans_temporary_file(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "note.txt"
    path.write_text("original", encoding="utf-8")

    def fail_replace(_source, _destination) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr(filesystem_io.os, "replace", fail_replace)
    with pytest.raises(ToolError, match="replace failed"):
        WorkspaceFiles(tmp_path).write_file(str(path), "updated", overwrite=True)

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
        WorkspaceFiles(tmp_path).write_file(str(link), "changed", overwrite=True)
    assert target.read_text(encoding="utf-8") == "target"


def test_glob_stops_walking_once_the_directory_bound_is_reached(tmp_path: Path) -> None:
    class BoundedFiles(WorkspaceFiles):
        _MAX_WALKED_FILES = 3

    for index in range(10):
        (tmp_path / f"file{index}.txt").write_text(str(index), encoding="utf-8")

    result = BoundedFiles(tmp_path).glob("**/*.txt")

    assert "directory search truncated" in result
    assert "file9.txt" not in result
