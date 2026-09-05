"""Definitions for workspace-confined filesystem tools."""

from __future__ import annotations

from ..base import Tool
from ..filesystem import WorkspaceFiles
from .schema import object_schema

_PATH_RULES = (
    "Use workspace:relative/path or project:relative/path. Approved absolute paths are also accepted. "
    "Bare paths use project when available, otherwise workspace; missing files never fall back to another root. "
)


def filesystem_read_tools(files: WorkspaceFiles) -> tuple[Tool, ...]:
    return (
        Tool(
            "read_file",
            (
                "Reads a bounded range from a UTF-8 text-file path inside an approved workspace or "
                "read-only Skill root, returning numbered normalized-LF lines and range metadata."
            ),
            files.read_file,
            object_schema(
                {
                    "path": {
                        "type": "string",
                        "minLength": 1,
                        "description": (
                            _PATH_RULES + "A UTF-8 text file, or an absolute path in an approved read-only Skill root."
                        ),
                    },
                    "start_line": {
                        "type": "integer",
                        "minimum": 1,
                        "default": 1,
                        "description": "The one-based line number at which reading starts. Defaults to 1.",
                    },
                    "start_column": {
                        "type": "integer",
                        "minimum": 1,
                        "default": 1,
                        "description": (
                            "The one-based column within start_line at which reading starts. Defaults to 1."
                        ),
                    },
                    "max_lines": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 1_000,
                        "default": 200,
                        "description": ("The maximum number of lines to return, from 1 to 1000. Defaults to 200."),
                    },
                },
                ["path"],
            ),
        ),
        Tool(
            "glob",
            (
                "Lists regular files under a selected directory whose relative paths match a case-sensitive glob "
                "pattern, returning sorted and bounded results."
            ),
            files.glob,
            object_schema(
                {
                    "pattern": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 4_096,
                        "description": (
                            "The case-sensitive glob pattern to match against relative file paths. Use forward "
                            "slashes; *, ?, and character sets match within one path segment, while ** matches "
                            "across directories."
                        ),
                    },
                    "path": {
                        "type": "string",
                        "minLength": 1,
                        "description": (
                            _PATH_RULES + "The directory to search. When omitted, both "
                            "available workspaces are searched."
                        ),
                    },
                    "max_results": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 1_000,
                        "default": 200,
                        "description": (
                            "The maximum number of matching file paths to return, from 1 to 1000. Defaults to 200."
                        ),
                    },
                },
                ["pattern"],
            ),
        ),
        Tool(
            "grep",
            (
                "Searches UTF-8 text files line by line for literal text or a regular expression and returns each "
                "match as path:line:text."
            ),
            files.grep,
            object_schema(
                {
                    "pattern": {
                        "type": "string",
                        "minLength": 1,
                        "description": "The literal text or regular expression to find.",
                    },
                    "path": {
                        "type": "string",
                        "minLength": 1,
                        "description": (
                            _PATH_RULES + "The file or directory to search. When omitted, "
                            "both available workspaces are searched."
                        ),
                    },
                    "glob": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 4_096,
                        "default": "**/*",
                        "description": (
                            "The case-sensitive glob pattern used to select files below path. Defaults to **/*."
                        ),
                    },
                    "regex": {
                        "type": "boolean",
                        "default": False,
                        "description": "Whether to interpret pattern as a regular expression. Defaults to false.",
                    },
                    "case_sensitive": {
                        "type": "boolean",
                        "default": True,
                        "description": "Whether text matching is case-sensitive. Defaults to true.",
                    },
                    "max_results": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 1_000,
                        "default": 200,
                        "description": (
                            "The maximum number of matching lines to return, from 1 to 1000. Defaults to 200."
                        ),
                    },
                },
                ["pattern"],
            ),
        ),
    )


def filesystem_mutation_tools(files: WorkspaceFiles) -> tuple[Tool, ...]:
    return (
        Tool(
            "create_directory",
            (
                "Creates a directory and any missing parent directories. Succeeds without changes if the "
                "directory already exists."
            ),
            files.create_directory,
            object_schema(
                {
                    "path": {
                        "type": "string",
                        "minLength": 1,
                        "description": _PATH_RULES + "The directory to create.",
                    },
                },
                ["path"],
            ),
            requires_confirmation=True,
            read_only=False,
            workspace_confined=True,
        ),
        Tool(
            "write_file",
            (
                "Creates a UTF-8 text file and any missing parent directories, or replaces the complete file when "
                "overwrite is true."
            ),
            files.write_file,
            object_schema(
                {
                    "path": {
                        "type": "string",
                        "minLength": 1,
                        "description": _PATH_RULES + "The file to write.",
                    },
                    "content": {
                        "type": "string",
                        "description": "The complete UTF-8 text to write to the file.",
                    },
                    "overwrite": {
                        "type": "boolean",
                        "default": False,
                        "description": (
                            "Whether to replace an existing file. Defaults to false; when false, an existing file "
                            "is rejected."
                        ),
                    },
                },
                ["path", "content"],
            ),
            requires_confirmation=True,
            read_only=False,
            workspace_confined=True,
        ),
        Tool(
            "edit_file",
            (
                "Replaces an inclusive line range in an existing UTF-8 text file after verifying the current "
                "line contents."
            ),
            files.edit_file,
            object_schema(
                {
                    "path": {
                        "type": "string",
                        "minLength": 1,
                        "description": _PATH_RULES + "The existing UTF-8 text file to edit.",
                    },
                    "start_line": {
                        "type": "integer",
                        "minimum": 1,
                        "description": "The one-based first line of the inclusive range to replace.",
                    },
                    "end_line": {
                        "type": "integer",
                        "minimum": 1,
                        "description": "The one-based last line of the inclusive range to replace.",
                    },
                    "expected_lines": {
                        "type": "array",
                        "minItems": 1,
                        "items": {"type": "string"},
                        "description": (
                            "The current unnumbered contents of every selected line, in order. The edit is "
                            "rejected if they no longer match."
                        ),
                    },
                    "replacement_lines": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "The replacement lines without line-break characters. Use an empty array to delete "
                            "the selected range or an array containing an empty string for one blank line."
                        ),
                    },
                },
                ["path", "start_line", "end_line", "expected_lines", "replacement_lines"],
            ),
            requires_confirmation=True,
            read_only=False,
            workspace_confined=True,
        ),
    )
