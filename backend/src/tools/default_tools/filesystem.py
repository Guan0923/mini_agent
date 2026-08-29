"""Definitions for workspace-confined filesystem tools."""

from __future__ import annotations

from ..base import Tool
from ..filesystem import WorkspaceFiles
from .schema import object_schema


def filesystem_read_tools(files: WorkspaceFiles) -> tuple[Tool, ...]:
    return (
        Tool(
            "read_file",
            "Reads a bounded range from a UTF-8 text file, returning normalized LF text and line-range metadata.",
            files.read_file,
            object_schema(
                {
                    "path": {
                        "type": "string",
                        "minLength": 1,
                        "description": (
                            "The path to the UTF-8 text file. Use a workspace-relative path, or an absolute."
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
                        "default": ".",
                        "description": "The directory to search. Defaults to the workspace root.",
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
                        "default": ".",
                        "description": "The file or directory to search. Defaults to the workspace root.",
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
                        "description": "The directory path to create.",
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
                        "description": "The file path to write.",
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
            "Edits an existing UTF-8 text file by replacing one uniquely matching text block with new text.",
            files.edit_file,
            object_schema(
                {
                    "path": {
                        "type": "string",
                        "minLength": 1,
                        "description": "The path to the existing UTF-8 text file.",
                    },
                    "old_text": {
                        "type": "string",
                        "minLength": 1,
                        "description": "The exact text to replace. It must occur exactly once in the file.",
                    },
                    "new_text": {
                        "type": "string",
                        "description": "The replacement text. Use an empty string to delete old_text.",
                    },
                },
                ["path", "old_text", "new_text"],
            ),
            requires_confirmation=True,
            read_only=False,
            workspace_confined=True,
        ),
    )


def upload_file_read_tool(files: WorkspaceFiles) -> Tool:
    """Read-only access to one session's uploaded files.

    Uploads live below the session workspace and must stay readable even when
    the agent's cwd is an external project folder, so they get their own
    confined, read-only tool instead of reusing the cwd-scoped ``read_file``.
    """

    return Tool(
        "read_upload_file",
        (
            "Reads a bounded range from a UTF-8 text file uploaded to the current session, returning normalized LF "
            "text and line-range metadata."
        ),
        files.read_file,
        object_schema(
            {
                "path": {
                    "type": "string",
                    "minLength": 1,
                    "description": (
                        "The path to the UTF-8 text file relative to the current session's upload directory."
                    ),
                },
                "start_line": {
                    "type": "integer",
                    "minimum": 1,
                    "default": 1,
                    "description": "The one-based line number at which reading starts. Defaults to 1.",
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
    )
