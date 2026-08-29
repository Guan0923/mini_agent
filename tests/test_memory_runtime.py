"""Model-free runtime adapters exercised with fixed memory fixtures."""

from __future__ import annotations

from pathlib import Path

from backend.configuration import ClientPaths
from backend.domain.memory import (
    MemoryCandidate,
    MemoryItem,
    MemoryJob,
    MemoryJobKind,
    MemoryJobStatus,
    MemoryKind,
    MemorySelectionDiff,
    MemoryWatermark,
)
from backend.runtime.memory import (
    MemoryConsolidator,
    MemoryEligibilityInput,
    MemoryEligibilityReason,
    MemoryExtractionRecorder,
    MemoryJobScheduler,
    MemoryRetriever,
    evaluate_memory_eligibility,
)
from backend.storage.memory import MemoryStore

T0 = "2026-01-01T00:00:00+00:00"
T1 = "2026-01-01T00:00:01+00:00"


def test_eligibility_is_pure_and_incremental() -> None:
    disabled = evaluate_memory_eligibility(MemoryEligibilityInput("session_a", 3, None, 20, False))
    assert disabled.reason is MemoryEligibilityReason.DISABLED
    no_events = evaluate_memory_eligibility(MemoryEligibilityInput("session_a", 3, 3, 20))
    assert no_events.reason is MemoryEligibilityReason.NO_NEW_EVENTS
    eligible = evaluate_memory_eligibility(MemoryEligibilityInput("session_a", 4, 3, 20))
    assert eligible.eligible
    assert (eligible.start_position, eligible.end_position) == (3, 4)


def test_runtime_adapters_record_retrieve_consolidate_and_schedule(tmp_path: Path) -> None:
    store = MemoryStore(ClientPaths(tmp_path / "user"))
    candidate = MemoryCandidate(
        candidate_id="candidate_a",
        kind=MemoryKind.SEMANTIC,
        content="fixed candidate",
        session_id="session_a",
        created_at=T0,
        updated_at=T0,
    )
    recorded = MemoryExtractionRecorder(store).record((candidate,), MemoryWatermark("session_a", 1, "event_1", T0))
    assert recorded == (candidate,)

    item = MemoryItem(
        memory_id="memory_a",
        kind=MemoryKind.SEMANTIC,
        title="Runtime fixed memory",
        content="retrievable infrastructure fact",
        created_at=T0,
        updated_at=T0,
    )
    result = MemoryConsolidator(store).apply(MemorySelectionDiff(added=(item,)))
    assert result.raw_projection is not None and result.raw_projection.is_file()
    assert MemoryRetriever(store).search("infrastructure")[0].item == item

    scheduler = MemoryJobScheduler(store)
    job = MemoryJob(
        job_id="memory_job_runtime",
        kind=MemoryJobKind.CONSOLIDATE,
        available_at=T0,
        created_at=T0,
        updated_at=T0,
    )
    scheduler.enqueue(job)
    claimed = scheduler.claim("worker_a", now=T0)
    assert claimed is not None and claimed.status is MemoryJobStatus.RUNNING
    assert scheduler.complete(job.job_id, "worker_a", completed_at=T1).status is MemoryJobStatus.SUCCEEDED
