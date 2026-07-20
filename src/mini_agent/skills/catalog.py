"""Workspace-local Skill discovery and validation."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from mini_agent.domain.skills import SkillSnapshot

SKILLS_RELATIVE_ROOT = Path(".mini_agent") / "skills"
MAX_SKILL_BYTES = 64 * 1024
MAX_INSTRUCTION_LINES = 500
_NAME_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_EXPLICIT_PATTERN = re.compile(r"(?<![\w$])\$([a-z0-9]+(?:-[a-z0-9]+)*)\b")


class SkillConfigurationError(ValueError):
    """Raised when a workspace Skill is unsafe or malformed."""


@dataclass(frozen=True)
class SkillDefinition:
    """One validated workspace Skill."""

    name: str
    description: str
    instructions: str
    root: str
    sha256: str

    def snapshot(self) -> SkillSnapshot:
        return SkillSnapshot(
            name=self.name,
            description=self.description,
            instructions=self.instructions,
            root=self.root,
            sha256=self.sha256,
        )


class SkillCatalog:
    """Stable, validated collection of project-local Skills."""

    def __init__(self, skills: tuple[SkillDefinition, ...] = ()) -> None:
        self._skills = skills
        self._by_name = {skill.name: skill for skill in skills}
        if len(self._by_name) != len(skills):
            raise SkillConfigurationError("Skill names must be unique.")

    @classmethod
    def discover(cls, workspace: Path) -> SkillCatalog:
        workspace_root = workspace.resolve()
        skills_root = workspace_root / SKILLS_RELATIVE_ROOT
        if not skills_root.exists():
            return cls()
        if not skills_root.is_dir():
            raise SkillConfigurationError(f"Skill root is not a directory: {skills_root}")

        resolved_root = cls._confined(skills_root, workspace_root, "Skill root")
        definitions: list[SkillDefinition] = []
        names: dict[str, Path] = {}
        directories = sorted(
            (item for item in skills_root.iterdir() if item.is_dir()),
            key=lambda item: item.name,
        )
        for directory in directories:
            resolved_directory = cls._confined(directory, resolved_root, "Skill directory")
            manifest = directory / "SKILL.md"
            if not manifest.exists():
                raise SkillConfigurationError(f"Skill directory is missing SKILL.md: {directory}")
            cls._confined(manifest, resolved_directory, "SKILL.md")
            definition = cls._load_manifest(manifest, directory.name, workspace_root)
            previous = names.get(definition.name)
            if previous is not None:
                raise SkillConfigurationError(
                    f"Duplicate Skill name {definition.name!r}: {previous} and {manifest}"
                )
            names[definition.name] = manifest
            definitions.append(definition)
        return cls(tuple(definitions))

    @staticmethod
    def _confined(path: Path, root: Path, label: str) -> Path:
        try:
            resolved = path.resolve(strict=True)
            resolved.relative_to(root)
        except (OSError, ValueError) as exc:
            raise SkillConfigurationError(f"{label} escapes its allowed root: {path}") from exc
        return resolved

    @classmethod
    def _load_manifest(cls, path: Path, directory_name: str, workspace: Path) -> SkillDefinition:
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise SkillConfigurationError(f"Cannot read Skill manifest {path}: {exc}") from exc
        if len(raw) > MAX_SKILL_BYTES:
            raise SkillConfigurationError(f"Skill manifest exceeds {MAX_SKILL_BYTES} bytes: {path}")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SkillConfigurationError(f"Skill manifest must be UTF-8: {path}") from exc

        lines = text.splitlines()
        if not lines or lines[0] != "---":
            raise SkillConfigurationError(f"Skill manifest must start with YAML frontmatter: {path}")
        try:
            closing = lines.index("---", 1)
        except ValueError as exc:
            raise SkillConfigurationError(f"Skill manifest has unclosed YAML frontmatter: {path}") from exc
        frontmatter_text = "\n".join(lines[1:closing])
        try:
            frontmatter = yaml.safe_load(frontmatter_text)
        except yaml.YAMLError as exc:
            raise SkillConfigurationError(f"Invalid YAML frontmatter in {path}: {exc}") from exc
        if not isinstance(frontmatter, dict):
            raise SkillConfigurationError(f"Skill frontmatter must be a mapping: {path}")
        if set(frontmatter) != {"name", "description"}:
            raise SkillConfigurationError(
                f"Skill frontmatter must contain only 'name' and 'description': {path}"
            )
        name = frontmatter["name"]
        description = frontmatter["description"]
        if not isinstance(name, str) or not name.strip():
            raise SkillConfigurationError(f"Skill name must be a non-empty string: {path}")
        if len(name) > 64 or _NAME_PATTERN.fullmatch(name) is None:
            raise SkillConfigurationError(
                "Skill name must use lowercase letters, digits, and hyphens "
                f"and be at most 64 characters: {path}"
            )
        if name != directory_name:
            raise SkillConfigurationError(
                f"Skill name {name!r} must match directory name {directory_name!r}: {path}"
            )
        if not isinstance(description, str) or not description.strip():
            raise SkillConfigurationError(f"Skill description must be a non-empty string: {path}")

        instruction_lines = lines[closing + 1 :]
        if len(instruction_lines) > MAX_INSTRUCTION_LINES:
            raise SkillConfigurationError(
                f"Skill instructions exceed {MAX_INSTRUCTION_LINES} lines: {path}"
            )
        instructions = "\n".join(instruction_lines).strip()
        if not instructions:
            raise SkillConfigurationError(f"Skill instructions must not be empty: {path}")
        relative_root = path.parent.resolve().relative_to(workspace).as_posix()
        return SkillDefinition(
            name=name,
            description=description.strip(),
            instructions=instructions,
            root=relative_root,
            sha256=hashlib.sha256(raw).hexdigest(),
        )

    def __bool__(self) -> bool:
        return bool(self._skills)

    def __len__(self) -> int:
        return len(self._skills)

    def definitions(self) -> tuple[SkillDefinition, ...]:
        return self._skills

    def names(self) -> tuple[str, ...]:
        return tuple(skill.name for skill in self._skills)

    def explicit_names(self, task: str) -> tuple[str, ...]:
        requested = {match.group(1) for match in _EXPLICIT_PATTERN.finditer(task)}
        return tuple(skill.name for skill in self._skills if skill.name in requested)

    def snapshots(self, names: set[str]) -> list[SkillSnapshot]:
        unknown = names - self._by_name.keys()
        if unknown:
            rendered = ", ".join(sorted(unknown))
            raise SkillConfigurationError(f"Unknown Skill selection: {rendered}")
        return [skill.snapshot() for skill in self._skills if skill.name in names]
