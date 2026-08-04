"""Tools capability tasks: reading, merging, and writing workspace files."""

from __future__ import annotations

from ..grading.programmatic import content_contains, files_exist, final_answer_contains, tool_used
from ..model import BenchmarkTask, Budgets, Seed, SeedFile

_ALPHA = "First alpha line.\nSecond alpha line.\n"
_BETA = "First beta line.\nSecond beta line.\n"

TASKS = (
    BenchmarkTask(
        name="tools-read-file",
        description="Read a seeded file and report its first line.",
        capability="tools",
        prompt="read notes/alpha.md",
        seed=Seed(files=(SeedFile("notes/alpha.md", _ALPHA),)),
        checkers=(
            final_answer_contains("First alpha line."),
            tool_used("read_file"),
        ),
        planner_modes=frozenset({"llm", "rule"}),
    ),
    BenchmarkTask(
        name="tools-extract-summary",
        description="Read two files and write a merged summary of their first lines.",
        capability="tools",
        prompt=(
            "read notes/alpha.md and notes/beta.md, then write notes/merged.md "
            "containing the first line of each file."
        ),
        seed=Seed(
            files=(
                SeedFile("notes/alpha.md", _ALPHA),
                SeedFile("notes/beta.md", _BETA),
            ),
        ),
        checkers=(
            files_exist("notes/merged.md"),
            content_contains("notes/merged.md", "First alpha line."),
            content_contains("notes/merged.md", "First beta line."),
            tool_used("read_file"),
            tool_used("write_file"),
        ),
        budgets=Budgets(max_model_turns=8, max_tool_calls=16),
        planner_modes=frozenset({"llm"}),
    ),
)
