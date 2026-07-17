"""Filesystem persistence for generated runtime artifacts."""

from __future__ import annotations

import hashlib
import os
import tempfile
import uuid
from pathlib import Path
from typing import Protocol

from mini_agent.domain import ArtifactMessage


class ArtifactStore(Protocol):
    """Create immutable artifacts produced by an agent run."""

    def create_plan(
        self,
        session_id: str,
        run_id: str,
        revision: int,
        content: str,
    ) -> ArtifactMessage: ...


class InMemoryArtifactStore:
    """Create self-contained artifacts without writing files."""

    def create_plan(
        self,
        session_id: str,
        run_id: str,
        revision: int,
        content: str,
    ) -> ArtifactMessage:
        if revision < 1:
            raise ValueError("Artifact revision must be positive.")
        del session_id
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        return ArtifactMessage(
            artifact_id=f"artifact_{uuid.uuid4().hex}",
            content=content,
            sha256=digest,
            revision=revision,
            created_by_run_id=run_id,
        )


class FileArtifactStore:
    """Persist plan artifacts beneath a workspace-local artifact directory."""

    def __init__(self, workspace: Path) -> None:
        self._workspace = workspace.resolve()
        self._artifact_root = (self._workspace / ".mini_agent" / "artifacts").resolve()
        try:
            self._artifact_root.relative_to(self._workspace)
        except ValueError as exc:
            raise ValueError("Artifact root escapes the configured workspace.") from exc

    def create_plan(
        self,
        session_id: str,
        run_id: str,
        revision: int,
        content: str,
    ) -> ArtifactMessage:
        if revision < 1:
            raise ValueError("Artifact revision must be positive.")

        target = self._plan_path(session_id, run_id, revision)
        target.parent.mkdir(parents=True, exist_ok=True)
        target = self._confined(target)
        encoded_content = content.encode("utf-8")
        if target.exists():
            if target.read_bytes() != encoded_content:
                raise FileExistsError(f"Artifact revision already exists: {target}")
        else:
            self._atomic_write(target, content)

        digest = hashlib.sha256(encoded_content).hexdigest()
        return ArtifactMessage(
            artifact_id=f"artifact_{uuid.uuid4().hex}",
            content=content,
            sha256=digest,
            revision=revision,
            created_by_run_id=run_id,
            relative_path=target.relative_to(self._workspace).as_posix(),
        )

    def _plan_path(self, session_id: str, run_id: str, revision: int) -> Path:
        self._validate_component(session_id, "session_id")
        self._validate_component(run_id, "run_id")
        return self._confined(self._artifact_root / session_id / run_id / f"plan-r{revision}.md")

    def _confined(self, path: Path) -> Path:
        resolved = path.resolve()
        try:
            resolved.relative_to(self._artifact_root)
        except ValueError as exc:
            raise ValueError("Artifact path escapes the configured artifact root.") from exc
        return resolved

    @staticmethod
    def _validate_component(value: str, name: str) -> None:
        component = Path(value)
        if (
            not value
            or value in {".", ".."}
            or "/" in value
            or "\\" in value
            or component.is_absolute()
            or len(component.parts) != 1
        ):
            raise ValueError(f"{name} must be a single path component.")

    @staticmethod
    def _atomic_write(target: Path, content: str) -> None:
        descriptor, temporary_name = tempfile.mkstemp(
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
        )
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, target)
        finally:
            temporary_path.unlink(missing_ok=True)
