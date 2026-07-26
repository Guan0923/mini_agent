"""Definitions for workspace-confined filesystem tools."""

from __future__ import annotations

from ..base import Tool
from ..filesystem import WorkspaceFiles
from .schema import object_schema


def filesystem_read_tools(files: WorkspaceFiles) -> tuple[Tool, ...]:
    return (
        Tool(
            "read_file",
            (
                "Reads a bounded line range from one UTF-8 text file inside the workspace. "
                "Output uses LF line endings and includes the returned and total line ranges. "
                "Use start_line and max_lines to continue through large files."
            ),
            files.read_file,
            object_schema(
                {
                    "path": {"type": "string", "minLength": 1, "description": "Workspace-relative file path."},
                    "start_line": {
                        "type": "integer",
                        "minimum": 1,
                        "default": 1,
                        "description": "One-based first line to return.",
                    },
                    "start_column": {
                        "type": "integer",
                        "minimum": 1,
                        "default": 1,
                        "description": "One-based column within start_line.",
                    },
                    "max_lines": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 1_000,
                        "default": 200,
                        "description": "Maximum number of lines to return.",
                    },
                },
                ["path"],
            ),
        ),
        Tool(
            "glob",
            (
                "Lists regular files inside the workspace whose relative paths match a case-sensitive glob. "
                "Use forward slashes; *, ?, and character sets match one path segment, while ** matches "
                "zero or more directories. Results are sorted and bounded."
            ),
            files.glob,
            object_schema(
                {
                    "pattern": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 4_096,
                        "description": "Workspace-relative glob pattern such as **/*.py.",
                    },
                    "path": {
                        "type": "string",
                        "minLength": 1,
                        "default": ".",
                        "description": "Workspace-relative directory to search.",
                    },
                    "max_results": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 1_000,
                        "default": 200,
                        "description": "Maximum number of file paths to return.",
                    },
                },
                ["pattern"],
            ),
        ),
        Tool(
            "grep",
            (
                "Searches UTF-8 workspace files line by line and returns path:line:text matches. "
                "Search is literal and case-sensitive by default; enable regex only when needed. "
                "Use the glob argument to restrict file paths. Binary, oversized, and non-UTF-8 files are skipped."
            ),
            files.grep,
            object_schema(
                {
                    "pattern": {
                        "type": "string",
                        "minLength": 1,
                        "description": "Literal text or regular expression to find.",
                    },
                    "path": {
                        "type": "string",
                        "minLength": 1,
                        "default": ".",
                        "description": "Workspace-relative file or directory to search.",
                    },
                    "glob": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 4_096,
                        "default": "**/*",
                        "description": "Case-sensitive glob applied below path.",
                    },
                    "regex": {
                        "type": "boolean",
                        "default": False,
                        "description": "Interpret pattern as a Python regular expression.",
                    },
                    "case_sensitive": {
                        "type": "boolean",
                        "default": True,
                        "description": "Match letter case.",
                    },
                    "max_results": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 1_000,
                        "default": 200,
                        "description": "Maximum number of matching lines to return.",
                    },
                },
                ["pattern"],
            ),
        ),
    )


def filesystem_mutation_tools(files: WorkspaceFiles) -> tuple[Tool, ...]:
    return (
        Tool(
            "write_file",
            (
                "Creates a UTF-8 text file inside the workspace. Existing files are rejected unless "
                "overwrite=true. Use edit_file for targeted changes; before replacing a complete existing "
                "file, read its current content and preserve everything that should remain."
            ),
            files.write_file,
            object_schema(
                {
                    "path": {
                        "type": "string",
                        "minLength": 1,
                        "description": "Workspace-relative destination file.",
                    },
                    "content": {"type": "string", "description": "Complete UTF-8 file content."},
                    "overwrite": {
                        "type": "boolean",
                        "default": False,
                        "description": "Explicitly replace an existing regular file.",
                    },
                },
                ["path", "content"],
            ),
            requires_confirmation=True,
            read_only=False,
        ),
        Tool(
            "edit_file",
            (
                "Edits an existing UTF-8 workspace file by replacing one exact text block. old_text must "
                "occur exactly once; zero or multiple matches fail without changes. Include enough surrounding "
                "context to make the match unique. Use an empty new_text to delete the matched block."
            ),
            files.edit_file,
            object_schema(
                {
                    "path": {
                        "type": "string",
                        "minLength": 1,
                        "description": "Workspace-relative existing file.",
                    },
                    "old_text": {
                        "type": "string",
                        "minLength": 1,
                        "description": "Exact, uniquely matching text to replace.",
                    },
                    "new_text": {"type": "string", "description": "Replacement text; may be empty."},
                },
                ["path", "old_text", "new_text"],
            ),
            requires_confirmation=True,
            read_only=False,
        ),
    )
