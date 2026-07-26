"""Serializable Skill values shared by the runtime and application layers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class SkillSnapshot:
    """Immutable instructions selected for one run."""

    name: str
    description: str
    instructions: str
    root: str
    sha256: str

    def to_dict(self) -> dict[str, str]:
        return {
            "name": self.name,
            "description": self.description,
            "instructions": self.instructions,
            "root": self.root,
            "sha256": self.sha256,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SkillSnapshot:
        return cls(
            name=str(data["name"]),
            description=str(data["description"]),
            instructions=str(data["instructions"]),
            root=str(data["root"]),
            sha256=str(data["sha256"]),
        )


@dataclass(frozen=True)
class SkillSelection:
    """Names selected by one planner activation request."""

    names: tuple[str, ...]
