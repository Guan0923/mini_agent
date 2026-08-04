"""Skills capability task: the agent must explicitly activate a seeded skill."""

from __future__ import annotations

from ..grading.programmatic import content_contains, files_exist, skill_activated
from ..model import BenchmarkTask, Budgets, Seed, SeedFile, SeedSkill

TASKS = (
    BenchmarkTask(
        name="skills-write-note",
        description="Explicitly use the $markdown-notes skill to write a meeting summary.",
        capability="skills",
        prompt=(
            "Use $markdown-notes to record a meeting summary of the seeded note into "
            "notes/summary.md. The summary must list at least three bullet points."
        ),
        seed=Seed(
            files=(SeedFile("notes/existing.md", "# Old note\n- stale item\n"),),
            skills=(
                SeedSkill(
                    name="markdown-notes",
                    description="Use for any note-taking and markdown formatting work.",
                    instructions=(
                        "# markdown-notes workflow\n"
                        "1. Always write notes as UTF-8 .md files under notes/.\n"
                        "2. Use `# H1` for the title and `- ` bullets for each item.\n"
                        "3. Finish every note with a line containing `(wrote via markdown-notes)`."
                    ),
                ),
            ),
        ),
        checkers=(
            files_exist("notes/summary.md"),
            content_contains("notes/summary.md", "- "),
            content_contains("notes/summary.md", "(wrote via markdown-notes)"),
            skill_activated("markdown-notes"),
        ),
        budgets=Budgets(max_model_turns=6, max_tool_calls=16),
        planner_modes=frozenset({"llm"}),
    ),
)
