"""Project-local Skill discovery."""

from .catalog import (
    MAX_INSTRUCTION_LINES,
    MAX_SKILL_BYTES,
    SKILLS_RELATIVE_ROOT,
    SkillCatalog,
    SkillConfigurationError,
    SkillDefinition,
)

__all__ = [
    "MAX_INSTRUCTION_LINES",
    "MAX_SKILL_BYTES",
    "SKILLS_RELATIVE_ROOT",
    "SkillCatalog",
    "SkillConfigurationError",
    "SkillDefinition",
]
