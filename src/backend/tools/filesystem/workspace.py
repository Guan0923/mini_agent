"""Workspace-confined file-tool composition."""

from __future__ import annotations

from pathlib import Path

from .io import FileIOMixin
from .paths import WorkspacePathMixin
from .reading import FileReadMixin
from .writing import FileWriteMixin


class WorkspaceFiles(FileReadMixin, FileWriteMixin, WorkspacePathMixin, FileIOMixin):
    """Perform deterministic text-file operations inside one workspace."""

    _MAX_READ_LINES = 1_000
    _MAX_OUTPUT_CHARS = 20_000
    _MAX_RESULTS = 1_000
    _MAX_SEARCH_FILE_BYTES = 2 * 1024 * 1024
    _MAX_MATCH_CHARS = 500
    _MAX_GLOB_PATTERN_CHARS = 4_096
    _MAX_GLOB_PATTERN_PARTS = 256
    _REGEX_TIMEOUT_SECONDS = 0.1
    _TEXT_CHUNK_CHARS = 8_192
    _IGNORED_DIRECTORIES = {
        ".git",
        ".mini_agent",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "__pycache__",
        "node_modules",
        "venv",
    }

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace.resolve()
