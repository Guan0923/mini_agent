import hashlib
from pathlib import Path

import pytest

from mini_agent.runtime import ArtifactStore, InMemoryArtifactStore
from mini_agent.storage import FileArtifactStore


def test_in_memory_store_embeds_plan_without_a_file_path() -> None:
    store: ArtifactStore = InMemoryArtifactStore()

    artifact = store.create_plan("session_1", "run_1", 1, "# Plan\n\nDo the work.")

    assert artifact.content == "# Plan\n\nDo the work."
    assert artifact.relative_path is None
    assert artifact.revision == 1
    assert artifact.created_by_run_id == "run_1"
    assert artifact.sha256 == hashlib.sha256(artifact.content.encode("utf-8")).hexdigest()
    assert artifact.artifact_id.startswith("artifact_")


def test_file_store_persists_a_utf8_plan_in_the_artifact_tree(tmp_path: Path) -> None:
    content = "# 计划\n\n执行工程化改造。"
    store: ArtifactStore = FileArtifactStore(tmp_path)

    artifact = store.create_plan("session_1", "run_1", 2, content)

    expected = tmp_path / ".mini_agent" / "artifacts" / "session_1" / "run_1" / "plan-r2.md"
    assert expected.read_text(encoding="utf-8") == content
    assert artifact.content == content
    assert artifact.relative_path == ".mini_agent/artifacts/session_1/run_1/plan-r2.md"
    assert artifact.sha256 == hashlib.sha256(content.encode("utf-8")).hexdigest()
    assert not list(expected.parent.glob("*.tmp"))


@pytest.mark.parametrize(
    ("session_id", "run_id"),
    [
        ("../outside", "run_1"),
        ("session_1", "../../outside"),
        ("session/subsession", "run_1"),
        ("session_1", "run\\outside"),
    ],
)
def test_file_store_rejects_artifact_path_escape(tmp_path: Path, session_id: str, run_id: str) -> None:
    store = FileArtifactStore(tmp_path)

    with pytest.raises(ValueError, match="single path component"):
        store.create_plan(session_id, run_id, 1, "unsafe")

    assert not (tmp_path.parent / "outside" / "plan-r1.md").exists()


def test_file_store_preserves_previous_revisions(tmp_path: Path) -> None:
    store = FileArtifactStore(tmp_path)
    first = store.create_plan("session_1", "run_1", 1, "first")
    second = store.create_plan("session_1", "run_1", 2, "second")

    assert (tmp_path / first.relative_path).read_text(encoding="utf-8") == "first"
    assert (tmp_path / second.relative_path).read_text(encoding="utf-8") == "second"
    assert first.artifact_id != second.artifact_id
    assert second.sha256 == hashlib.sha256(b"second").hexdigest()


def test_file_store_does_not_overwrite_an_existing_revision(tmp_path: Path) -> None:
    store = FileArtifactStore(tmp_path)
    artifact = store.create_plan("session_1", "run_1", 1, "original")

    with pytest.raises(FileExistsError, match="already exists"):
        store.create_plan("session_1", "run_1", 1, "changed")

    assert (tmp_path / artifact.relative_path).read_text(encoding="utf-8") == "original"


def test_file_store_requires_a_positive_revision(tmp_path: Path) -> None:
    stores: list[ArtifactStore] = [InMemoryArtifactStore(), FileArtifactStore(tmp_path)]

    for store in stores:
        with pytest.raises(ValueError, match="positive"):
            store.create_plan("session_1", "run_1", 0, "invalid")
