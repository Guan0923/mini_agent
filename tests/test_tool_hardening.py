"""Regression tests for bounded and security-sensitive tool behavior."""

import os
import re
import stat
from pathlib import Path

import pytest

from backend.tools import ToolError, ToolRegistry, WorkspaceFiles


def _payload(result: str) -> str:
    return result.split("\n", 1)[1].split("\n... output truncated", 1)[0]


def test_read_file_resumes_one_long_line_without_losing_content(tmp_path: Path) -> None:
    path = tmp_path / "long.txt"
    path.write_text("x" * 45_000 + "\nsecond\n", encoding="utf-8")
    files = WorkspaceFiles(tmp_path)

    column = 1
    reconstructed = ""
    while True:
        result = files.read_file(str(path), start_line=1, start_column=column, max_lines=1)
        body = _payload(result)
        assert body.startswith("1 | ")
        reconstructed += body.removeprefix("1 | ")
        continuation = re.search(r"continue with start_line=1 and start_column=(\d+)", result)
        if continuation is None:
            break
        column = int(continuation.group(1))

    assert reconstructed == "x" * 45_000 + "\n"


def test_read_file_validates_columns_and_streams_universal_newlines(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "note.txt"
    path.write_bytes(b"one\r\ntwo")
    monkeypatch.setattr(WorkspaceFiles, "_TEXT_CHUNK_CHARS", 1)
    files = WorkspaceFiles(tmp_path)

    assert files.read_file(str(path), start_line=1, start_column=2, max_lines=2) == (
        f"{path.resolve().as_posix()}: lines 1-2 of 2, starting at column 2\n1 | ne\n2 | two"
    )
    with pytest.raises(ToolError, match="start_column"):
        files.read_file(str(path), start_line=1, start_column=5)


def test_iterative_glob_supports_deep_valid_patterns_and_rejects_excessive_depth(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("print('ok')", encoding="utf-8")
    files = WorkspaceFiles(tmp_path)
    valid_pattern = "/".join(["**"] * 200 + ["*.py"])

    assert files.glob(valid_pattern) == (tmp_path / "app.py").resolve().as_posix()
    with pytest.raises(ToolError, match="path segments"):
        files.glob("/".join(["**"] * 257))


@pytest.mark.parametrize("tool", ["glob", "grep"])
def test_registry_converts_excessive_glob_inputs_to_tool_errors(tmp_path: Path, tool: str) -> None:
    registry = ToolRegistry(tmp_path)
    arguments = (
        {"pattern": "/".join(["**"] * 257)}
        if tool == "glob"
        else {
            "pattern": "needle",
            "glob": "/".join(["**"] * 257),
        }
    )

    with pytest.raises(ToolError, match="path segments"):
        registry.invoke(tool, arguments)

    long_arguments = (
        {"pattern": "x" * 4_097}
        if tool == "glob"
        else {
            "pattern": "needle",
            "glob": "x" * 4_097,
        }
    )
    with pytest.raises(ToolError, match="Invalid arguments"):
        registry.invoke(tool, long_arguments)


def test_grep_regex_timeout_becomes_a_tool_error(tmp_path: Path) -> None:
    (tmp_path / "pathological.txt").write_text("a" * 50_000 + "!", encoding="utf-8")

    with pytest.raises(ToolError, match="timed out"):
        WorkspaceFiles(tmp_path).grep(r"^(a+)+$", regex=True)


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits are not meaningful on Windows.")
def test_overwrite_and_edit_preserve_posix_permissions(tmp_path: Path) -> None:
    path = tmp_path / "script.sh"
    path.write_text("old\n", encoding="utf-8")
    path.chmod(0o751)
    files = WorkspaceFiles(tmp_path)

    files.write_file(str(path), "replacement\n", overwrite=True)
    assert stat.S_IMODE(path.stat().st_mode) == 0o751

    files.edit_file(str(path), 1, 1, ["replacement"], ["edited"])
    assert stat.S_IMODE(path.stat().st_mode) == 0o751
