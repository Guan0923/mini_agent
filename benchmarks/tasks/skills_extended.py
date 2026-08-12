"""A second Skill workflow with a distinct output contract."""

from __future__ import annotations

from ..grading.programmatic import content_contains, files_exist, skill_activated, status_completed
from ..model import BenchmarkTask, Budgets, Seed, SeedFile, SeedSkill

TASKS = (
    BenchmarkTask(
        name="skills-release-notes",
        description="Activate a release-notes Skill and produce a structured changelog.",
        capability="skills",
        prompt=(
            "Use $release-notes to turn changes.md into CHANGELOG.md. Keep the facts from the source, "
            "organize them under Added and Fixed headings, and follow every instruction in the Skill."
        ),
        seed=Seed(
            files=(SeedFile("changes.md", "- Added offline benchmark reports\n- Fixed stale chat spinners\n"),),
            skills=(
                SeedSkill(
                    name="release-notes",
                    description="Use for changelogs and release communication.",
                    instructions=(
                        "# release-notes workflow\n"
                        "1. Read the source changes before writing.\n"
                        "2. Write CHANGELOG.md with `## Added` and `## Fixed` sections.\n"
                        "3. Preserve the source facts and finish with `(wrote via release-notes)`."
                    ),
                ),
            ),
        ),
        checkers=(
            status_completed,
            files_exist("CHANGELOG.md"),
            content_contains("CHANGELOG.md", "## Added"),
            content_contains("CHANGELOG.md", "## Fixed"),
            content_contains("CHANGELOG.md", "stale chat spinners"),
            content_contains("CHANGELOG.md", "(wrote via release-notes)"),
            skill_activated("release-notes"),
        ),
        budgets=Budgets(max_tool_calls=16),
        planner_modes=frozenset({"llm"}),
    ),
)
