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
    source: str = "user"
    project_id: str | None = None
    tree_sha256: str | None = None

    def to_dict(self) -> dict[str, str]:
        data: dict[str, str] = {
            "name": self.name,
            "description": self.description,
            "instructions": self.instructions,
            "root": self.root,
            "sha256": self.sha256,
            "source": self.source,
        }
        if self.project_id is not None:
            data["project_id"] = self.project_id
        if self.tree_sha256 is not None:
            data["tree_sha256"] = self.tree_sha256
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SkillSnapshot:
        return cls(
            name=str(data["name"]),
            description=str(data["description"]),
            instructions=str(data["instructions"]),
            root=str(data["root"]),
            sha256=str(data["sha256"]),
            source=str(data.get("source") or "user"),
            project_id=str(data["project_id"]) if data.get("project_id") else None,
            tree_sha256=str(data["tree_sha256"]) if data.get("tree_sha256") else None,
        )


@dataclass(frozen=True)
class SkillSelection:
    """Names selected by one planner activation request."""

    names: tuple[str, ...]
