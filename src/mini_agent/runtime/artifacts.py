"""Compatibility exports for artifact stores now owned by ``storage``."""

from mini_agent.storage.artifacts import ArtifactStore, InMemoryArtifactStore

__all__ = ["ArtifactStore", "InMemoryArtifactStore"]
