"""HTTPS synchronization client and lifecycle coordinator."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Protocol
from urllib.parse import urlparse

import requests

from backend.jobs import AdmissionPolicy, JobLane, JobRegistry, JobScopeKind, ThreadJob

from .events import decrypt_event_batch, encrypt_event_batch

_LOG = logging.getLogger(__name__)


class SyncTransport(Protocol):
    def post(self, path: str, payload: dict[str, object]) -> dict[str, object]: ...

    def get(self, path: str) -> dict[str, object]: ...


class RequestsSyncTransport:
    def __init__(self, base_url: str, token: str, device_id: str, *, timeout: float = 10.0) -> None:
        parsed = urlparse(base_url)
        if parsed.scheme.lower() != "https":
            raise ValueError("sync.url must use HTTPS.")
        if (
            not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("sync.url must be an HTTPS endpoint without credentials, query, or fragment.")
        self._base_url = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {token}", "X-Device-ID": device_id}
        self._timeout = timeout

    def post(self, path: str, payload: dict[str, object]) -> dict[str, object]:
        response = requests.post(f"{self._base_url}{path}", json=payload, headers=self._headers, timeout=self._timeout)
        response.raise_for_status()
        value = response.json()
        if not isinstance(value, dict):
            raise ValueError("Sync service returned a non-object response.")
        return value

    def get(self, path: str) -> dict[str, object]:
        response = requests.get(f"{self._base_url}{path}", headers=self._headers, timeout=self._timeout)
        response.raise_for_status()
        value = response.json()
        if not isinstance(value, dict):
            raise ValueError("Sync service returned a non-object response.")
        return value


class SyncClient:
    def __init__(self, device_id: str, transport: SyncTransport, key_provider=None) -> None:
        self.device_id = device_id
        self.transport = transport
        self.key_provider = key_provider

    def synchronize(self, store) -> None:
        operations = store.pending_sync_operations()
        if operations:
            acknowledgements: list[dict[str, object]] = []
            for operation in operations:
                session_id = str(operation["session_id"])
                events = operation.get("events", [])
                if not isinstance(events, list):
                    raise ValueError("Local sync operation has invalid events.")
                if self.key_provider is None:
                    raise ValueError("An authenticated user DEK provider is required for event synchronization.")
                key = self.key_provider(session_id)
                event_ids = [str(item["event_id"]) for item in events if isinstance(item, dict) and item.get("event_id")]
                envelope = encrypt_event_batch([item for item in events if isinstance(item, dict)], key, aad=session_id)
                result = self.transport.post(
                    "/v1/sync/push",
                    {
                        "session_id": session_id,
                        "parent_revision": int(operation.get("base_revision", 0)),
                        "device_id": self.device_id,
                        "event_id": str(operation["operation_id"]),
                        "event_ids": event_ids,
                        "envelope": envelope,
                        "checksum": str(envelope["checksum"]),
                    },
                )
                acknowledgements.append(
                    {
                        "session_id": session_id,
                        "event_ids": event_ids,
                        "revision": int(result.get("revision", result.get("head_revision", 0))),
                    }
                )
            store.acknowledge_sync_operations(acknowledgements)
        # Local project conversations never participate in cloud sync.  Keep
        # them out of the pull cursor as well as the push outbox so their
        # session identifiers are not disclosed to the sync service.
        known = {
            summary.session_id: store.remote_revision(summary.session_id)
            for summary in store.list_sessions(state="all")
            if not summary.local_only
        }
        pull = getattr(self.transport, "get", None)
        if not callable(pull):
            return
        for session_id, revision in known.items():
            result = pull(f"/v1/sync/pull?session_id={session_id}&after_revision={revision}")
            raw_events = result.get("events", [])
            if not isinstance(raw_events, list):
                continue
            for item in raw_events:
                if not isinstance(item, dict):
                    continue
                if self.key_provider is None:
                    raise ValueError("An authenticated user DEK provider is required for event synchronization.")
                key = self.key_provider(session_id)
                events = decrypt_event_batch(item.get("envelope", {}), key, aad=session_id)
                store.apply_sync_events(
                    {
                        "session_id": session_id,
                        "revision": int(item.get("revision", 0)),
                        "owner_device_id": self.device_id,
                        "parent_revision": int(item.get("parent_revision", revision - 1)),
                        "events": events,
                    },
                    local_device_id=self.device_id,
                )


class SyncCoordinator:
    """One event-driven worker; there is deliberately no periodic polling."""

    def __init__(
        self,
        client: SyncClient,
        store,
        *,
        error_sink: Callable[[str], None] | None = None,
        job_registry: JobRegistry | None = None,
    ) -> None:
        self.client = client
        self.store = store
        self.error_sink = error_sink
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="mini-agent-sync", daemon=True)
        self._registry = job_registry
        self._job: ThreadJob | None = None
        self._started = False

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        if self._registry is None:
            self._thread.start()
        else:
            scope = self._registry.root_scope().child(JobScopeKind.RUNNER)
            self._job = ThreadJob(self._registry.new_job_id(), self._run)
            self._registry.submit(
                self._job,
                scope=scope,
                lane=JobLane.BACKGROUND,
                admission=AdmissionPolicy(),
            )
        self.notify()

    def notify(self) -> None:
        self._wake.set()

    def close(self, timeout: float = 5.0) -> None:
        if not self._started:
            return
        self._stop.set()
        self._wake.set()
        if self._job is not None:
            self._job.cancel("sync coordinator closed")
            self._job.wait(timeout)
        else:
            self._thread.join(timeout)

    def _run(self) -> None:
        while True:
            self._wake.wait()
            self._wake.clear()
            stopping = self._stop.is_set()
            self._sync_once()
            if stopping:
                return

    def _sync_once(self) -> None:
        try:
            self.client.synchronize(self.store)
        except Exception as exc:
            category = type(exc).__name__
            _LOG.warning("sync_failed error=%s", category)
            if self.error_sink is not None:
                self.error_sink(category)
