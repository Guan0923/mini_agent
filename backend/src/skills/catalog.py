"""User-level Skill discovery and validation."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from backend.domain import safe_error_message
from backend.domain.skills import SkillSnapshot

MAX_SKILL_BYTES = 64 * 1024
MAX_INSTRUCTION_LINES = 1_000
MAX_METADATA_BYTES = 2 * 1024
_NAME_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_EXPLICIT_PATTERN = re.compile(r"(?<![\w$])\$([a-z0-9]+(?:-[a-z0-9]+)*)\b")


class SkillConfigurationError(ValueError):
    """Raised when a workspace Skill is unsafe or malformed."""


def read_manifest_bytes(path: Path) -> bytes:
    """Read a SKILL.md with size and encoding guards (shared by discovery and snapshot)."""
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise SkillConfigurationError(safe_error_message(exc)) from exc
    if len(raw) > MAX_SKILL_BYTES:
        raise SkillConfigurationError(f"Skill manifest exceeds {MAX_SKILL_BYTES} bytes: {path}")
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SkillConfigurationError(safe_error_message(exc)) from exc
    return raw


def parse_manifest(raw: bytes, path: Path) -> tuple[dict[str, object], list[str]]:
    """Split a SKILL.md into its YAML frontmatter and the body lines after it."""
    text = raw.decode("utf-8")
    lines = text.splitlines()
    if not lines or lines[0] != "---":
        raise SkillConfigurationError(f"Skill manifest must start with YAML frontmatter: {path}")
    try:
        closing = lines.index("---", 1)
    except ValueError as exc:
        raise SkillConfigurationError(safe_error_message(exc)) from exc
    frontmatter_text = "\n".join(lines[1:closing])
    try:
        frontmatter = yaml.safe_load(frontmatter_text)
    except yaml.YAMLError as exc:
        raise SkillConfigurationError(safe_error_message(exc)) from exc
    if not isinstance(frontmatter, dict):
        raise SkillConfigurationError(f"Skill frontmatter must be a mapping: {path}")
    return frontmatter, lines[closing + 1 :]


def validate_metadata(value: object, path: Path) -> tuple[tuple[str, str], ...]:
    """Validate the optional metadata mapping (string keys mapped to string values)."""
    if not isinstance(value, dict):
        raise SkillConfigurationError(f"Skill metadata must be a mapping: {path}")
    items: list[tuple[str, str]] = []
    for key, item in value.items():
        if not isinstance(key, str) or not key.strip():
            raise SkillConfigurationError(f"Skill metadata keys must be non-empty strings: {path}")
        if not isinstance(item, str):
            raise SkillConfigurationError(f"Skill metadata values must be strings: {path}")
        items.append((key, item))
    serialized = yaml.safe_dump(dict(items), allow_unicode=True).encode("utf-8")
    if len(serialized) > MAX_METADATA_BYTES:
        raise SkillConfigurationError(f"Skill metadata exceeds {MAX_METADATA_BYTES} bytes: {path}")
    return tuple(items)


def validate_allowed_tools(value: object, path: Path) -> tuple[str, ...]:
    """Validate the optional allowed-tools list (non-empty strings only)."""
    if not isinstance(value, list):
        raise SkillConfigurationError(f"Skill allowed-tools must be a list of strings: {path}")
    tools: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise SkillConfigurationError(f"Skill allowed-tools must contain only non-empty strings: {path}")
        tools.append(item)
    return tuple(tools)


@dataclass(frozen=True)
class _SkillFrontmatter:
    """Supported values selected from an otherwise open frontmatter mapping."""

    name: str
    description: str
    metadata: tuple[tuple[str, str], ...]
    allowed_tools: tuple[str, ...]


def select_frontmatter(path: Path, directory_name: str, **kwargs: object) -> _SkillFrontmatter | None:
    """Select supported Skill fields while ignoring every other frontmatter key."""
    name = kwargs.get("name")
    description = kwargs.get("description")
    if not isinstance(name, str) or not name.strip():
        return None
    if len(name) > 64 or _NAME_PATTERN.fullmatch(name) is None or name != directory_name:
        return None
    if not isinstance(description, str) or not description.strip():
        return None

    try:
        metadata = validate_metadata(kwargs["metadata"], path) if "metadata" in kwargs else ()
    except SkillConfigurationError:
        metadata = ()
    try:
        allowed_tools = validate_allowed_tools(kwargs["allowed-tools"], path) if "allowed-tools" in kwargs else ()
    except SkillConfigurationError:
        allowed_tools = ()

    return _SkillFrontmatter(
        name=name,
        description=description.strip(),
        metadata=metadata,
        allowed_tools=allowed_tools,
    )


@dataclass(frozen=True)
class SkillDefinition:
    """One validated workspace Skill (frontmatter only; instructions load lazily)."""

    name: str
    description: str
    metadata: tuple[tuple[str, str], ...]
    allowed_tools: tuple[str, ...]
    root: str
    manifest: Path

    def snapshot(self) -> SkillSnapshot:
        """Read and validate instructions now that the Skill is selected."""
        raw = read_manifest_bytes(self.manifest)
        _frontmatter, body_lines = parse_manifest(raw, self.manifest)
        if len(body_lines) > MAX_INSTRUCTION_LINES:
            raise SkillConfigurationError(f"Skill instructions exceed {MAX_INSTRUCTION_LINES} lines: {self.manifest}")
        instructions = "\n".join(body_lines).strip()
        if not instructions:
            raise SkillConfigurationError(f"Skill instructions must not be empty: {self.manifest}")
        return SkillSnapshot(
            name=self.name,
            description=self.description,
            instructions=instructions,
            root=self.root,
            sha256=hashlib.sha256(raw).hexdigest(),
        )


class SkillCatalog:
    """Stable, validated collection of project-local Skills."""

    def __init__(self, skills: tuple[SkillDefinition, ...] = ()) -> None:
        self._skills = skills
        self._by_name = {skill.name: skill for skill in skills}
        if len(self._by_name) != len(skills):
            raise SkillConfigurationError("Skill names must be unique.")

    @classmethod
    def discover(cls, root: Path | None = None, *, global_root: Path | None = None) -> SkillCatalog:
        """Discover Skills from the canonical user-level root.

        ``root`` is the single source of user Skills.  ``global_root`` is kept
        as a deprecated alias for compatibility and takes precedence when both
        are provided.
        """

        definitions: dict[str, SkillDefinition] = {}
        skills_root = global_root if global_root is not None else root
        if skills_root is not None:
            definitions.update(cls._discover_root(skills_root, skills_root, absolute_roots=True))
        return cls(tuple(definitions[name] for name in sorted(definitions)))

    @classmethod
    def _discover_root(
        cls,
        skills_root: Path,
        owner_root: Path,
        *,
        absolute_roots: bool = False,
    ) -> dict[str, SkillDefinition]:
        if not skills_root.exists():
            return {}
        if not skills_root.is_dir():
            raise SkillConfigurationError(f"Skill root is not a directory: {skills_root}")
        resolved_root = cls._confined(skills_root, owner_root, "Skill root")
        definitions: dict[str, SkillDefinition] = {}
        for directory in sorted((item for item in skills_root.iterdir() if item.is_dir()), key=lambda item: item.name):
            resolved_directory = cls._confined(directory, resolved_root, "Skill directory")
            manifest = directory / "SKILL.md"
            if not manifest.exists():
                raise SkillConfigurationError(f"Skill directory is missing SKILL.md: {directory}")
            cls._confined(manifest, resolved_directory, "SKILL.md")
            definition = cls._load_manifest(manifest, directory.name, owner_root, absolute_root=absolute_roots)
            if definition is None:
                continue
            if definition.name in definitions:
                raise SkillConfigurationError(f"Duplicate Skill name {definition.name!r}: {manifest}")
            definitions[definition.name] = definition
        return definitions

    @staticmethod
    def _confined(path: Path, root: Path, label: str) -> Path:
        try:
            resolved = path.resolve(strict=True)
            resolved.relative_to(root)
        except (OSError, ValueError) as exc:
            raise SkillConfigurationError(safe_error_message(exc)) from exc
        return resolved

    @classmethod
    def _load_manifest(
        cls, path: Path, directory_name: str, workspace: Path, *, absolute_root: bool = False
    ) -> SkillDefinition | None:
        raw = read_manifest_bytes(path)
        frontmatter, _body_lines = parse_manifest(raw, path)
        selected = select_frontmatter(path, directory_name, **frontmatter)
        if selected is None:
            return None
        resolved_skill_root = path.parent.resolve()
        display_root = (
            resolved_skill_root.as_posix() if absolute_root else resolved_skill_root.relative_to(workspace).as_posix()
        )
        return SkillDefinition(
            name=selected.name,
            description=selected.description,
            metadata=selected.metadata,
            allowed_tools=selected.allowed_tools,
            root=display_root,
            manifest=resolved_skill_root / "SKILL.md",
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
        unknown = requested - self._by_name.keys()
        if unknown:
            missing = ", ".join(sorted(unknown))
            available = ", ".join(self.names()) or "none"
            raise SkillConfigurationError(
                f"Unknown explicit Skill reference: {missing}. Available Skills: {available}."
            )
        return tuple(skill.name for skill in self._skills if skill.name in requested)

    def snapshots(self, names: set[str]) -> list[SkillSnapshot]:
        unknown = names - self._by_name.keys()
        if unknown:
            rendered = ", ".join(sorted(unknown))
            raise SkillConfigurationError(f"Unknown Skill selection: {rendered}")
        return [skill.snapshot() for skill in self._skills if skill.name in names]
