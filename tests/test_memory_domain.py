"""Focused validation tests for provider-independent memory values."""

from __future__ import annotations

from dataclasses import replace

import pytest

from backend.domain.memory import (
    MemoryCandidate,
    MemoryItem,
    MemoryJob,
    MemoryJobKind,
    MemoryJobStatus,
    MemoryKind,
    MemoryScope,
    MemorySelectionDiff,
)


def test_memory_item_supports_all_kinds_and_enforces_scope() -> None:
    for kind in MemoryKind:
        item = MemoryItem.new(kind=kind, title=kind.value, content=f"fixed {kind.value} memory")
        assert item.kind is kind
        assert item.scope is MemoryScope.GLOBAL

    project = MemoryItem.new(
        kind=MemoryKind.PROCEDURAL,
        title="Build",
        content="Run the fixed build command.",
        scope=MemoryScope.PROJECT,
        project_id="project_a",
    )
    assert project.project_id == "project_a"
    with pytest.raises(ValueError, match="require project_id"):
        replace(project, project_id=None)
    with pytest.raises(ValueError, match="cannot set project_id"):
        replace(project, scope=MemoryScope.GLOBAL)


@pytest.mark.parametrize("unsafe", ["../escape", "a/b", "a\\b", ".", "", "has space"])
def test_memory_identifiers_reject_traversal_and_unsafe_values(unsafe: str) -> None:
    with pytest.raises(ValueError):
        MemoryCandidate.new(kind=MemoryKind.EPISODIC, content="fixed", session_id=unsafe)
    with pytest.raises(ValueError):
        MemoryItem.new(
            kind=MemoryKind.SEMANTIC,
            title="fixed",
            content="fixed",
            scope=MemoryScope.PROJECT,
            project_id=unsafe,
        )


def test_memory_timestamps_and_job_leases_are_canonical() -> None:
    job = MemoryJob.new(kind=MemoryJobKind.EXTRACT)
    with pytest.raises(ValueError, match="time zone"):
        replace(job, available_at="2026-01-01T00:00:00")
    with pytest.raises(ValueError, match="canonical UTC"):
        replace(job, available_at="2026-01-01T08:00:00+08:00")
    with pytest.raises(ValueError, match="Only running"):
        replace(job, lease_owner="worker_a", lease_expires_at="2026-01-01T00:00:00+00:00")
    running = replace(
        job,
        status=MemoryJobStatus.RUNNING,
        lease_owner="worker_a",
        lease_expires_at="2026-01-01T00:00:00+00:00",
    )
    assert running.status is MemoryJobStatus.RUNNING


def test_selection_diff_categories_are_unique_and_disjoint() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        MemorySelectionDiff(retained_ids=("memory_a", "memory_a"))
    with pytest.raises(ValueError, match="disjoint"):
        MemorySelectionDiff(retained_ids=("memory_a",), removed_ids=("memory_a",))
