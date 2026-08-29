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

    def list_heads(self) -> list[dict[str, object]]: ...


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

    def list_heads(self) -> list[dict[str, object]]:
        payload = self.get("/v1/sync/heads")
        heads = payload.get("heads", [])
        return [dict(item) for item in heads if isinstance(item, dict)] if isinstance(heads, list) else []


class SyncClient:
    def __init__(self, device_id: str, transport: SyncTransport, key_provider=None) -> None:
        self.device_id = device_id
        self.transport = transport
        self.key_provider = key_provider

    def synchronize(self, store) -> None:
        operations = store.pending_sync_operations()
        if operations:
            for operation in operations:
                self._push_operation(store, operation)
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
        if hasattr(self.transport, "list_heads") or callable(getattr(self.transport, "heads", None)):
            heads = self.transport.list_heads() if hasattr(self.transport, "list_heads") else self.transport.heads()
            if isinstance(heads, list):
                for head in heads:
                    if isinstance(head, dict) and head.get("session_id"):
                        session_id = str(head["session_id"])
                        known.setdefault(session_id, store.remote_revision(session_id) if session_id in known else 0)
        for session_id, revision in list(known.items()):
            self._pull_session(store, session_id, revision)

    def _push_operation(self, store, operation: dict[str, object]) -> None:
        session_id = str(operation["session_id"])
        events = operation.get("events", [])
        if not isinstance(events, list) or not all(isinstance(item, dict) for item in events):
            raise ValueError("Local sync operation has invalid events.")
        if self.key_provider is None:
            raise ValueError("An authenticated user DEK provider is required for event synchronization.")
        event_ids = [str(item["event_id"]) for item in events if item.get("event_id")]
        key = self.key_provider(session_id)
        envelope = encrypt_event_batch(events, key, aad=session_id)
        payload = {
            "session_id": session_id,
            "parent_revision": int(operation.get("base_revision", 0)),
            "device_id": self.device_id,
            "event_id": str(operation["operation_id"]),
            "event_ids": event_ids,
            "envelope": envelope,
            "checksum": str(envelope["checksum"]),
        }
        try:
            result = self.transport.post("/v1/sync/push", payload)
        except Exception as exc:
            if getattr(exc, "status_code", None) != 409:
                raise
            self._pull_session(store, session_id, int(operation.get("base_revision", 0)))
            refreshed = next(
                (item for item in store.pending_sync_operations() if item.get("session_id") == session_id), None
            )
            if refreshed is None:
                return
            payload["parent_revision"] = int(refreshed.get("base_revision", 0))
            result = self.transport.post("/v1/sync/push", payload)
        revision = int(result.get("revision", result.get("head_revision", 0)))
        store.acknowledge_sync_operations([{"session_id": session_id, "event_ids": event_ids, "revision": revision}])

    def _pull_session(self, store, session_id: str, revision: int) -> None:
        result = self.transport.get(f"/v1/sync/pull?session_id={session_id}&after_revision={revision}")
        raw_events = result.get("events", [])
        if not isinstance(raw_events, list):
            return
        if self.key_provider is None:
            raise ValueError("An authenticated user DEK provider is required for event synchronization.")
        key = self.key_provider(session_id)
        for item in raw_events:
            if not isinstance(item, dict):
                continue
            events = decrypt_event_batch(item.get("envelope", {}), key, aad=session_id)
            store.apply_sync_events(
                {
                    "session_id": session_id,
                    "revision": int(item.get("revision", 0)),
                    "owner_device_id": str(item.get("device_id") or self.device_id),
                    "parent_revision": int(item.get("parent_revision", revision)),
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
