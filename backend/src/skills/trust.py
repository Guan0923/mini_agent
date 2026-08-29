"""Per-Skill project trust persisted in the user ``config.toml``.

Project Skills are untrusted repository content.  Trust is granted per
Skill name for the exact full-directory tree hash at the exact project
path; any other combination is untrusted.  The store keeps only hashes and
timestamps in ``[project_skill_trust]`` — never Skill instructions,
resource contents, or secrets — and fails closed on malformed data.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

from backend.configuration import ConfigurationError, UserConfigStore
from backend.runtime.core.contracts import InterruptDecision, InterruptRequest

from .project import ProjectSkillDefinition, discover_project_skills

_SECTION = "project_skill_trust"


def workspace_sha256(cwd: Path) -> str:
    """Stable path identity used to bind per-project trust records."""
    normalized = os.path.normcase(str(cwd.resolve())).replace("\\", "/")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


class ProjectSkillTrustStore:
    """Read/write per-Skill trust records inside one user's config.toml."""

    def __init__(self, config_store: UserConfigStore) -> None:
        self._config = config_store

    # -- reads -------------------------------------------------------------

    def is_trusted(self, project_id: str, workspace_sha256: str, skill_name: str, tree_sha256: str) -> bool:
        record = self._load_project(project_id)
        if record is None or record.get("workspace_sha256") != workspace_sha256:
            return False
        skills = record.get("skills", {})
        entry = skills.get(skill_name)
        return isinstance(entry, Mapping) and entry.get("tree_sha256") == tree_sha256

    def trusted_skills(self, project_id: str, workspace_sha256: str) -> dict[str, str]:
        """Map Skill name -> tree hash for one trusted project path."""
        record = self._load_project(project_id)
        if record is None or record.get("workspace_sha256") != workspace_sha256:
            return {}
        skills = record.get("skills", {})
        return {
            str(name): str(entry.get("tree_sha256"))
            for name, entry in skills.items()
            if isinstance(entry, Mapping) and isinstance(entry.get("tree_sha256"), str) and entry["tree_sha256"]
        }

    # -- writes ------------------------------------------------------------

    def record_trust(self, project_id: str, workspace_sha256: str, skill_name: str, tree_sha256: str) -> None:
        """Persist trust for exactly one Skill tree version."""
        self._validate(project_id, skill_name, tree_sha256)
        state = self._load_state()
        projects = state.setdefault("projects", {})
        project = projects.setdefault(project_id, {"workspace_sha256": workspace_sha256})
        if project.get("workspace_sha256") != workspace_sha256:
            project = {"workspace_sha256": workspace_sha256}
            projects[project_id] = project
        skills = project.setdefault("skills", {})
        skills[skill_name] = {"tree_sha256": tree_sha256, "trusted_at": self._now()}
        self._write(state)

    def revoke_skill(self, project_id: str, skill_name: str) -> None:
        state = self._load_state()
        projects = state.setdefault("projects", {})
        project = projects.get(project_id)
        if not isinstance(project, Mapping):
            self._write(state)
            return
        skills = project.get("skills", {})
        if isinstance(skills, Mapping):
            skills.pop(skill_name, None)
        self._write(state)

    def revoke_project(self, project_id: str) -> None:
        state = self._load_state()
        projects = state.setdefault("projects", {})
        projects.pop(project_id, None)
        self._write(state)

    # -- internals ---------------------------------------------------------

    @staticmethod
    def _validate(project_id: str, skill_name: str, tree_sha256: str) -> None:
        if not project_id or "/" in project_id or "\\" in project_id:
            raise ValueError("Invalid project id.")
        if not skill_name or "/" in skill_name or "\\" in skill_name:
            raise ValueError("Invalid Skill name.")
        if not isinstance(tree_sha256, str) or len(tree_sha256) != 64:
            raise ValueError("tree_sha256 must be a 64-character hex string.")

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat()

    def _load_state(self) -> dict[str, object]:
        values = self._config.read()
        section = values.get(_SECTION, {})
        if not isinstance(section, Mapping):
            raise ConfigurationError(f"[{_SECTION}] must be a table.")
        return {str(key): dict(value) if isinstance(value, Mapping) else value for key, value in section.items()}

    def _load_project(self, project_id: str) -> Mapping | None:
        projects = self._load_state().get("projects", {})
        if not isinstance(projects, Mapping):
            return None
        record = projects.get(project_id)
        return record if isinstance(record, Mapping) else None

    def _write(self, state: Mapping[str, object]) -> None:
        self._config.replace_section(_SECTION, dict(state))


class ProjectSkillGateResult:
    """Trust-filtered project Skill candidates for one run."""

    __slots__ = ("usable", "untrusted_names")

    def __init__(self, usable: list[ProjectSkillDefinition], untrusted_names: list[str]) -> None:
        self.usable = usable
        self.untrusted_names = untrusted_names


class ProjectSkillGate:
    """Validate project Skill trust before any Skill reaches the model.

    The gate scans the untrusted project Skill layer, filters candidates
    whose full-directory tree hash matches the user's trust records, and
    asks the human one Skill at a time for every untrusted or changed
    candidate.  Without an interrupt handler, untrusted Skills are skipped
    (fail closed) — never auto-trusted, even under full-access tool mode.
    """

    def __init__(
        self,
        workspace: Path,
        project_id: str,
        trust: ProjectSkillTrustStore,
        *,
        interactive: bool = True,
    ) -> None:
        self.workspace = workspace
        self.project_id = project_id
        self.trust = trust
        self.interactive = interactive
        self._workspace_sha = workspace_sha256(workspace)

    def prepare(self, runtime) -> ProjectSkillGateResult:
        """Return trusted project Skills, approving each untrusted one once."""
        if self.project_id is None or self.project_id == "":
            return ProjectSkillGateResult([], [])
        candidates = discover_project_skills(self.workspace, self.project_id)
        usable: list[ProjectSkillDefinition] = []
        untrusted: list[str] = []
        for candidate in candidates:
            if self.trust.is_trusted(self.project_id, self._workspace_sha, candidate.name, candidate.tree_sha256):
                usable.append(candidate)
                continue
            untrusted.append(candidate.name)
            try:
                decision = self._ask(runtime, candidate)
            except Exception:
                # An exception from the interrupt channel (timeouts, missing
                # handler) must never activate an untrusted Skill.
                continue
            if decision.choice == "trust":
                self.trust.record_trust(self.project_id, self._workspace_sha, candidate.name, candidate.tree_sha256)
                usable.append(candidate)
                untrusted.pop()
            # "skip" leaves the Skill invisible to this run and the model.
        return ProjectSkillGateResult(usable, untrusted)

    def _ask(self, runtime, candidate: ProjectSkillDefinition) -> InterruptDecision:
        if not self.interactive:
            return InterruptDecision("skip")
        interrupt = getattr(getattr(runtime, "services", None), "interrupt", None)
        if not callable(interrupt):
            return InterruptDecision("skip")
        request = InterruptRequest(
            kind="skill",
            message=f"Project Skill {candidate.name!r} is not trusted.",
            data={
                "skill": candidate.name,
                "description": candidate.description,
                "project_id": self.project_id,
                "workspace_sha256": self._workspace_sha,
                "tree_sha256": candidate.tree_sha256,
                "path": candidate.root,
            },
        )
        return interrupt(request)
