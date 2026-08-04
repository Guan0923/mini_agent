"""Additional deterministic workspace-tool benchmark tasks."""

from __future__ import annotations

from ..grading.programmatic import (
    content_equals,
    final_answer_contains,
    status_completed,
    tool_used,
)
from ..model import BenchmarkTask, Budgets, Seed, SeedFile

_CONFIG_BEFORE = "MODE=dev\nFEATURE_FLAG=off\nKEEP_THIS_LINE=unchanged\n"
_CONFIG_AFTER = "MODE=dev\nFEATURE_FLAG=on\nKEEP_THIS_LINE=unchanged\n"
_NUMBERS = "7\n11\n24\n"


TASKS = (
    BenchmarkTask(
        name="tools-list-files",
        description="Discover nested workspace files and report their paths.",
        capability="tools",
        prompt="list files in the workspace and report the discovered paths",
        seed=Seed(
            files=(
                SeedFile("docs/guide.md", "# Guide\n"),
                SeedFile("src/app.py", "print('hello')\n"),
                SeedFile("notes/README.md", "# Notes\n"),
            )
        ),
        checkers=(
            status_completed,
            final_answer_contains("docs/guide.md"),
            final_answer_contains("src/app.py"),
            tool_used("glob"),
        ),
        planner_modes=frozenset({"llm", "rule"}),
    ),
    BenchmarkTask(
        name="tools-search-edit",
        description="Find one configuration value and edit only that exact value.",
        capability="tools",
        prompt=(
            "Use glob and grep to find the only config file containing FEATURE_FLAG=off, then use edit_file "
            "to change only that value to FEATURE_FLAG=on. Preserve every other line."
        ),
        seed=Seed(files=(SeedFile("config/app.env", _CONFIG_BEFORE),)),
        checkers=(
            status_completed,
            content_equals("config/app.env", _CONFIG_AFTER),
            tool_used("glob"),
            tool_used("grep"),
            tool_used("edit_file"),
        ),
        budgets=Budgets(max_model_turns=8, max_tool_calls=16),
        planner_modes=frozenset({"llm"}),
    ),
    BenchmarkTask(
        name="tools-command-sum",
        description="Use the command tool to calculate a deterministic sum without changing input.",
        capability="tools",
        prompt=(
            "Use run_command to calculate the sum of the integers in data/numbers.txt. Report the total "
            "in your final answer and do not modify the input file."
        ),
        seed=Seed(files=(SeedFile("data/numbers.txt", _NUMBERS),)),
        checkers=(
            status_completed,
            final_answer_contains("42"),
            content_equals("data/numbers.txt", _NUMBERS),
            tool_used("run_command"),
        ),
        budgets=Budgets(max_model_turns=6, max_tool_calls=12),
        planner_modes=frozenset({"llm"}),
    ),
)
