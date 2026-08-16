"""Un-trusted project Skill candidates and per-Skill directory fingerprints.

Project Skills live in ``<workspace>/.mini_agent/skills/<name>`` and are
treated as untrusted repository content.  Nothing from this module is ever
merged into the runtime catalog or sent to the model before the user
approves the exact Skill tree; approval is persisted per Skill name against
its complete directory-tree SHA-256.
"""

from __future__ import annotations

import hashlib
import stat
from dataclasses import dataclass
from pathlib import Path

from backend.domain.skills import SkillSnapshot

from .catalog import (
    _ALLOWED_FRONTMATTER_KEYS,
    _NAME_PATTERN,
    SkillConfigurationError,
    parse_manifest,
    read_manifest_bytes,
    validate_allowed_tools,
    validate_metadata,
)

MAX_PROJECT_SKILLS = 64
MAX_FILES_PER_SKILL = 256
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_TREE_BYTES = 16 * 1024 * 1024
MAX_PROJECT_TREE_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True)
class ProjectSkillDefinition:
    """One validated candidate Skill from an untrusted project repository.

    The instructions are loaded only through :meth:`snapshot` (the same lazy
    path used by user Skills), but callers must not snapshot an unapproved
    project Skill and must not send its name/description/tools to the model
    before approval.
    """

    name: str
    description: str
    metadata: tuple[tuple[str, str], ...]
    allowed_tools: tuple[str, ...]
    project_id: str
    tree_sha256: str
    root: str
    manifest: Path

    @property
    def source(self) -> str:
        return "project"

    def snapshot(self) -> SkillSnapshot:
        raw = read_manifest_bytes(self.manifest)
        _frontmatter, body_lines = parse_manifest(raw, self.manifest)
        if len(body_lines) > 1_000:
            raise SkillConfigurationError(f"Skill instructions exceed 1000 lines: {self.manifest}")
        instructions = "\n".join(body_lines).strip()
        if not instructions:
            raise SkillConfigurationError(f"Skill instructions must not be empty: {self.manifest}")
        return SkillSnapshot(
            name=self.name,
            description=self.description,
            instructions=instructions,
            root=self.root,
            sha256=hashlib.sha256(raw).hexdigest(),
            source="project",
            project_id=self.project_id,
            tree_sha256=self.tree_sha256,
        )


def discover_project_skills(workspace: Path, project_id: str) -> tuple[ProjectSkillDefinition, ...]:
    """Scan one untrusted project for Skill candidates without trusting them.

    A malformed or oversized Skill disables only that Skill; other valid
    candidates and the user-level catalog remain available.  Raises
    :class:`SkillConfigurationError` only for structural problems that make
    the whole candidate layer unsafe to read (root not a directory, a Skill
    directory escaping the root, or an unreadable tree).
    """

    skills_root = (workspace / ".mini_agent" / "skills").resolve()
    if not skills_root.exists():
        return ()
    if not skills_root.is_dir():
        raise SkillConfigurationError(f"Project Skill root is not a directory: {skills_root}")

    definitions: list[ProjectSkillDefinition] = []
    for directory in sorted((item for item in skills_root.iterdir()), key=lambda item: item.name):
        if not directory.is_dir():
            # Non-directory entries under the Skill root are suspicious, but
            # they cannot activate a Skill.  Skip them instead of failing the
            # entire candidate layer.
            continue
        try:
            resolved = directory.resolve(strict=True)
            resolved.relative_to(skills_root)
        except (OSError, ValueError) as exc:
            raise SkillConfigurationError(f"Project Skill directory escapes its root: {directory}") from exc
        definition = _scan_skill_directory(resolved, project_id)
        if definition is not None:
            definitions.append(definition)
        if len(definitions) >= MAX_PROJECT_SKILLS:
            break

    total_bytes = sum(len(skill.tree_sha256) for skill in definitions)
    if total_bytes > MAX_PROJECT_TREE_BYTES:
        raise SkillConfigurationError("Project Skill layer exceeds the total size limit.")
    return tuple(definitions)


def _scan_skill_directory(directory: Path, project_id: str) -> ProjectSkillDefinition | None:
    manifest = directory / "SKILL.md"
    if not manifest.exists():
        # A directory without SKILL.md cannot be a Skill; ignore it.
        return None

    # ---- Tree fingerprint -------------------------------------------------
    entries: list[tuple[str, int, bytes]] = []
    total_bytes = 0
    try:
        for path in directory.rglob("*"):
            if path.is_symlink():
                raise SkillConfigurationError(f"Project Skill contains a symbolic link: {path}")
            if path.is_dir():
                continue
            try:
                file_stat = path.stat()
            except OSError as exc:
                raise SkillConfigurationError(f"Cannot stat project Skill file: {path}") from exc
            if not stat.S_ISREG(file_stat.st_mode):
                raise SkillConfigurationError(f"Project Skill contains a non-regular file: {path}")
            relative = path.relative_to(directory).as_posix()
            if len(entries) >= MAX_FILES_PER_SKILL:
                raise SkillConfigurationError(f"Project Skill exceeds {MAX_FILES_PER_SKILL} files: {directory}")
            size = file_stat.st_size
            if size > MAX_FILE_BYTES:
                raise SkillConfigurationError(f"Project Skill file exceeds {MAX_FILE_BYTES} bytes: {path}")
            data = path.read_bytes()
            total_bytes += len(data)
            if total_bytes > MAX_TREE_BYTES:
                raise SkillConfigurationError(f"Project Skill exceeds {MAX_TREE_BYTES} bytes: {directory}")
            entries.append((relative, len(data), data))
    except OSError as exc:
        raise SkillConfigurationError(f"Cannot read project Skill tree: {directory}") from exc

    tree_hasher = hashlib.sha256()
    for relative, size, data in sorted(entries, key=lambda item: item[0]):
        tree_hasher.update(relative.encode("utf-8"))
        tree_hasher.update(b"\x00")
        tree_hasher.update(str(size).encode("ascii"))
        tree_hasher.update(b"\x00")
        tree_hasher.update(data)
    tree_sha256 = tree_hasher.hexdigest()

    # ---- Manifest validation (per-Skill; malformed Skill is skipped) ------
    try:
        raw = read_manifest_bytes(manifest)
    except SkillConfigurationError:
        return None
    try:
        frontmatter, _body_lines = parse_manifest(raw, manifest)
    except SkillConfigurationError:
        return None
    if not isinstance(frontmatter, dict):
        return None
    unknown = set(frontmatter) - _ALLOWED_FRONTMATTER_KEYS
    if unknown:
        return None
    if "name" not in frontmatter or "description" not in frontmatter:
        return None
    name = frontmatter["name"]
    description = frontmatter["description"]
    if not isinstance(name, str) or not name.strip():
        return None
    if len(name) > 64 or _NAME_PATTERN.fullmatch(name) is None:
        return None
    if name != directory.name:
        return None
    if not isinstance(description, str) or not description.strip():
        return None
    try:
        metadata = validate_metadata(frontmatter["metadata"], manifest) if "metadata" in frontmatter else ()
        allowed_tools = (
            validate_allowed_tools(frontmatter["allowed-tools"], manifest)
            if "allowed-tools" in frontmatter
            else ()
        )
    except SkillConfigurationError:
        return None

    return ProjectSkillDefinition(
        name=name,
        description=description.strip(),
        metadata=metadata,
        allowed_tools=allowed_tools,
        project_id=project_id,
        tree_sha256=tree_sha256,
        root=directory.as_posix(),
        manifest=manifest,
    )
