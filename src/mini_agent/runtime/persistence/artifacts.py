"""Storage boundary and in-memory adapter for generated artifacts."""

from __future__ import annotations

import hashlib
import uuid
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
