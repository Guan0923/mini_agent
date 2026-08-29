"""Automatic Memory scheduling and failure-isolation tests."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event
from uuid import uuid4

from backend.configuration import ClientPaths
from backend.domain.memory import MemoryJobStatus, MemoryKind
from backend.jobs import JobRegistry
from backend.runtime.memory import (
    MemoryAutomationService,
    MemoryAutomationSettings,
    MemoryModelUnavailable,
)
from backend.storage.memory import MemoryStore
from backend.storage.sqlite import SQLiteSessionStore


class _Settings:
    def __init__(self, *, automatic_memory_enabled: bool = True) -> None:
        self.automatic_memory_enabled = automatic_memory_enabled

    def memory_config_for_user(self, _user_id: str):
        return {
            "generate_memories": True,
            "automatic_memory_enabled": self.automatic_memory_enabled,
            "disable_on_external_context": True,
        }


class _Projects:
    @staticmethod
    def session_project(_session_id: str):
        return None


class _MemoryModel:
    def extract_episodic(self, request):
        evidence_id = [value.message_id for value in request.messages if value.role == "user"][-1]
        return {
            "candidates": [
                {
                    "title": "Stable preference",
                    "content": "The user prefers concise technical reports.",
                    "summary": "Concise reports",
                    "confidence": 0.9,
                    "tags": ["preference"],
                    "evidence_message_ids": [evidence_id],
                    "rediscoverable_from_source": False,
                }
            ]
        }

    def consolidate_memories(self, request):
        candidate_ids = [value.candidate_id for value in request.candidates]
        return {
            "added": [
                {
                    "kind": "semantic",
                    "title": "Stable preference",
                    "content": "The user prefers concise technical reports.",
                    "summary": "Concise reports",
                    "scope": "global",
                    "project_id": None,
                    "confidence": 0.95,
                    "tags": ["preference"],
                    "candidate_ids": candidate_ids,
                }
            ],
            "retained": [],
            "removed": [],
            "rejected_candidate_ids": [],
        }


class _State:
    def __init__(self, root: Path, *, automatic_memory_enabled: bool = True) -> None:
        self.data_root = root
        self.settings = _Settings(automatic_memory_enabled=automatic_memory_enabled)
        self.job_registry = JobRegistry()
        self.system_job_scope = self.job_registry.root_scope()
        self.event_sync_manager = None

    def user_paths(self, user_id: str) -> ClientPaths:
        return ClientPaths(self.data_root / user_id)

    @staticmethod
    def projects(_user_id: str):
        return _Projects()

    @staticmethod
    def mark_sync_dirty(_user_id: str) -> None:
        return None


def _conversation(state: _State, user_id: str) -> str:
    store = SQLiteSessionStore(state.user_paths(user_id), f"web_{user_id}")
    session = store.import_conversation(
        "Memory automation fixture",
        [
            {
                "role": "user",
                "content": "Please remember that all future technical reports should be concise and direct.",
            },
            {"role": "assistant", "content": "Understood."},
            {"role": "user", "content": "This is a durable preference that should remain available in later sessions."},
            {"role": "assistant", "content": "Preference acknowledged."},
        ],
    )
    return session.session_id


def _wait_for(store: MemoryStore, status: MemoryJobStatus, *, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if any(job.status is status for job in store.list_jobs(limit=100)):
            return
        time.sleep(0.01)
    raise AssertionError(f"Memory job did not reach {status.value}.")


def test_idle_session_runs_phase1_then_globally_serial_phase2(tmp_path: Path) -> None:
    state = _State(tmp_path / "web")
    user_id = str(uuid4())
    session_id = _conversation(state, user_id)
    service = MemoryAutomationService(
        state,
        lambda _user_id: _MemoryModel(),
        settings=MemoryAutomationSettings(idle_seconds=0, scan_interval_seconds=60),
        clock=lambda: datetime(2030, 1, 1, tzinfo=UTC),
    )
    store = MemoryStore(state.user_paths(user_id))
    try:
        service.scan_once()
        _wait_for(store, MemoryJobStatus.SUCCEEDED)
        service.scan_once()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            semantic = store.list_items(kinds=(MemoryKind.SEMANTIC,), limit=10)
            if semantic:
                break
            time.sleep(0.01)
        else:
            raise AssertionError("Phase 2 did not create semantic memory.")
        assert store.get_watermark(session_id) is not None
        assert semantic[0].content == "The user prefers concise technical reports."
    finally:
        state.job_registry.close_all(timeout=2)


def test_manual_generation_runs_before_automatic_idle_scanning_is_enabled(tmp_path: Path) -> None:
    state = _State(tmp_path / "web", automatic_memory_enabled=False)
    user_id = str(uuid4())
    session_id = _conversation(state, user_id)
    service = MemoryAutomationService(
        state,
        lambda _user_id: _MemoryModel(),
        settings=MemoryAutomationSettings(idle_seconds=0, scan_interval_seconds=60),
        clock=lambda: datetime(2030, 1, 1, tzinfo=UTC),
    )
    store = MemoryStore(state.user_paths(user_id))
    try:
        service.scan_once()
        assert store.list_jobs(limit=100) == []
        assert store.get_watermark(session_id) is None

        service.enqueue_extract(user_id, session_id)
        service.scan_once()
        _wait_for(store, MemoryJobStatus.SUCCEEDED)
        assert store.get_watermark(session_id) is not None

        second_session_id = _conversation(state, user_id)
        state.settings.automatic_memory_enabled = True
        service.scan_once()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if store.get_watermark(second_session_id) is not None:
                break
            time.sleep(0.01)
        else:
            raise AssertionError("Automatic Memory did not process the idle session after opt-in.")
    finally:
        state.job_registry.close_all(timeout=2)


def test_unavailable_model_cancels_memory_job_without_raising(tmp_path: Path) -> None:
    state = _State(tmp_path / "web")
    user_id = str(uuid4())
    session_id = _conversation(state, user_id)

    def unavailable(_user_id: str):
        raise MemoryModelUnavailable("provider_unavailable")

    service = MemoryAutomationService(
        state,
        unavailable,
        settings=MemoryAutomationSettings(idle_seconds=0, scan_interval_seconds=60),
        clock=lambda: datetime(2030, 1, 1, tzinfo=UTC),
    )
    store = MemoryStore(state.user_paths(user_id))
    try:
        service.enqueue_extract(user_id, session_id)
        service.scan_once()
        _wait_for(store, MemoryJobStatus.CANCELLED)
        assert store.get_watermark(session_id) is None
    finally:
        state.job_registry.close_all(timeout=2)


def test_retry_uses_a_new_process_job_id_and_preserves_the_root_error(tmp_path: Path) -> None:
    state = _State(tmp_path / "web", automatic_memory_enabled=False)
    user_id = str(uuid4())
    session_id = _conversation(state, user_id)

    class FlakyModel(_MemoryModel):
        calls = 0

        def extract_episodic(self, request):
            self.calls += 1
            if self.calls == 1:
                getattr(object(), "missing_for_memory_test")
            return super().extract_episodic(request)

    model = FlakyModel()
    service = MemoryAutomationService(
        state,
        lambda _user_id: model,
        settings=MemoryAutomationSettings(
            idle_seconds=0,
            scan_interval_seconds=60,
            retry_base_seconds=1,
            retry_max_seconds=1,
        ),
        clock=lambda: datetime.now(UTC) - timedelta(seconds=10),
    )
    store = MemoryStore(state.user_paths(user_id))
    try:
        persisted = service.enqueue_extract(user_id, session_id)
        service.scan_once()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            current = store.get_job(persisted.job_id)
            if current is not None and current.status is MemoryJobStatus.PENDING and current.attempts == 1:
                break
            time.sleep(0.01)
        else:
            raise AssertionError("Memory job did not enter retry state.")
        assert current.last_error == "AttributeError:missing_for_memory_test"

        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            service.scan_once()
            current = store.get_job(persisted.job_id)
            if current is not None and current.status is MemoryJobStatus.SUCCEEDED:
                break
            time.sleep(0.01)
        else:
            raise AssertionError("Retried Memory job did not succeed.")

        assert model.calls == 2
        assert current is not None and current.attempts == 2
        assert state.job_registry.get_for_user(user_id, f"{persisted.job_id}_attempt_1") is not None
        assert state.job_registry.get_for_user(user_id, f"{persisted.job_id}_attempt_2") is not None
    finally:
        state.job_registry.close_all(timeout=2)


def test_cancellation_during_model_call_prevents_late_memory_write(tmp_path: Path) -> None:
    state = _State(tmp_path / "web")
    user_id = str(uuid4())
    session_id = _conversation(state, user_id)
    started = Event()
    release = Event()

    class BlockingModel(_MemoryModel):
        def extract_episodic(self, request):
            started.set()
            assert release.wait(2)
            return super().extract_episodic(request)

    service = MemoryAutomationService(
        state,
        lambda _user_id: BlockingModel(),
        settings=MemoryAutomationSettings(idle_seconds=0, scan_interval_seconds=60),
        clock=lambda: datetime(2030, 1, 1, tzinfo=UTC),
    )
    store = MemoryStore(state.user_paths(user_id))
    try:
        job = service.enqueue_extract(user_id, session_id)
        service.scan_once()
        assert started.wait(2)
        service.cancel(user_id, job.job_id)
        release.set()
        _wait_for(store, MemoryJobStatus.CANCELLED)
        time.sleep(0.05)
        assert store.list_items(include_deleted=True) == []
        assert store.get_watermark(session_id) is None
    finally:
        release.set()
        state.job_registry.close_all(timeout=2)
