"""A deterministic parent/subagent collaboration benchmark."""

from __future__ import annotations

from ..grading.programmatic import (
    content_contains,
    files_exist,
    status_completed,
    subagents_completed,
    subagents_failed,
    tool_used,
)
from ..model import BenchmarkTask, Budgets, Seed, SeedFile

TASKS = (
    BenchmarkTask(
        name="subagents-parallel-summary",
        description="Delegate two independent file reads and combine both facts.",
        capability="subagents",
        prompt=(
            "Delegate two independent tasks with delegate_tasks: one child reads research/alpha.md and "
            "one child reads research/beta.md, and each returns its key fact. After both children finish, "
            "write notes/combined.md containing both facts, including the ALPHA_FACT and BETA_FACT markers."
        ),
        seed=Seed(
            files=(
                SeedFile("research/alpha.md", "Alpha source marker: ALPHA_FACT\n"),
                SeedFile("research/beta.md", "Beta source marker: BETA_FACT\n"),
            )
        ),
        checkers=(
            status_completed,
            files_exist("notes/combined.md"),
            content_contains("notes/combined.md", "ALPHA_FACT"),
            content_contains("notes/combined.md", "BETA_FACT"),
            tool_used("delegate_tasks"),
            subagents_completed(2),
            subagents_failed(0),
        ),
        budgets=Budgets(max_model_turns=10, max_tool_calls=32, max_replans=3),
        planner_modes=frozenset({"llm"}),
    ),
)
