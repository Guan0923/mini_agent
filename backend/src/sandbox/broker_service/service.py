"""Authenticated Broker request dispatch and resource recovery."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from threading import RLock
from typing import Any

from ..errors import (
    SandboxCleanupPending,
    SandboxError,
    SandboxFailureCode,
    SandboxInitializationError,
)
from ..runtime.manifest import ResourceManifest, ResourceRecord
from ..runtime.reclaimer import SandboxResourceReclaimer
from .configuration import BrokerConfiguration
from .credentials import DpapiKeyStore
from .installer import BrokerProcessAdapter
from .protocol import BROKER_VERSION, MAX_CLOCK_SKEW_SECONDS, MAX_REQUEST_TTL_SECONDS, _canonical

logger = logging.getLogger(__name__)


class WindowsBrokerService:
    """Authenticated request dispatcher used by the standalone Broker."""

    def __init__(
        self,
        configuration: BrokerConfiguration,
        *,
        key_store: DpapiKeyStore | None = None,
        adapter: BrokerProcessAdapter | None = None,
        installed: bool = True,
        is_windows: bool | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.configuration = configuration
        self.key_store = key_store or DpapiKeyStore(configuration.installation_key_path)
        self.adapter = adapter
        self.installed = installed
        self.is_windows = os.name == "nt" if is_windows is None else is_windows
        self._clock = clock or time.time
        self.manifest = ResourceManifest(
            configuration.manifest_path,
            installation_id=configuration.installation_id,
            backend_instance_id=configuration.backend_instance_id,
        )
        self._key: bytes | None = None
        self._nonces: dict[str, int] = {}
        self._pending_releases: dict[tuple[str, str, str], dict[str, Any]] = {}
        self._lock = RLock()
        self._reclaimer = SandboxResourceReclaimer(self._recover_pending_releases)

    def initialize(self) -> None:
        if not self.is_windows:
            raise SandboxInitializationError("Windows Broker is unavailable on this platform")
        self.configuration.persist_installation_id()
        self._key = self.key_store.ensure()
        if self.adapter is None:
            from ..native_broker_adapter.adapter import WindowsNativeBrokerAdapter
            from ..native_windows import WindowsPowerShellWfpController

            self.adapter = WindowsNativeBrokerAdapter(wfp=WindowsPowerShellWfpController())
        if self.manifest.records():
            self.recover_orphans(set())
        self._reclaimer.start()

    def close(self) -> None:
        self._reclaimer.close()

    def handle(self, payload: bytes) -> bytes:
        """Verify one request and return one authenticated response."""

        try:
            request = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise SandboxInitializationError("Broker request is invalid") from exc
        if not isinstance(request, dict):
            raise SandboxInitializationError("Broker request is invalid")
        operation = request.get("operation")
        nonce = request.get("nonce")
        issued_at = request.get("issued_at")
        expires_at = request.get("expires_at")
        signature = request.get("hmac")
        body = request.get("body", {})
        if not isinstance(operation, str) or not operation or not isinstance(nonce, str) or not nonce:
            raise SandboxInitializationError("Broker request envelope is invalid")
        if (
            not isinstance(signature, str)
            or not isinstance(body, Mapping)
            or isinstance(issued_at, bool)
            or not isinstance(issued_at, int)
            or isinstance(expires_at, bool)
            or not isinstance(expires_at, int)
        ):
            raise SandboxInitializationError("Broker request authentication is invalid")
        now = int(self._clock())
        if issued_at > now + MAX_CLOCK_SKEW_SECONDS:
            raise SandboxInitializationError("Broker request timestamp is invalid")
        if expires_at < now or expires_at <= issued_at:
            raise SandboxInitializationError("Broker request has expired")
        if expires_at - issued_at > MAX_REQUEST_TTL_SECONDS:
            raise SandboxInitializationError("Broker request lifetime is invalid")
        key = self._key or self.key_store.load()
        unsigned = {
            "operation": operation,
            "nonce": nonce,
            "issued_at": issued_at,
            "expires_at": expires_at,
            "body": dict(body),
        }
        expected = hmac.new(key, _canonical(unsigned), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise SandboxInitializationError("Broker request authentication failed")
        with self._lock:
            self._nonces = {value: expiry for value, expiry in self._nonces.items() if expiry >= now}
            if nonce in self._nonces:
                raise SandboxInitializationError("Broker request replay detected")
            self._nonces[nonce] = expires_at
        try:
            result = self._dispatch(operation, dict(body))
        except Exception as exc:
            self._audit(operation, "failed", body)
            code = exc.code if isinstance(exc, SandboxError) else SandboxFailureCode.INIT_FAILED
            result = {"error": {"code": str(code)}}
        else:
            self._audit(operation, "succeeded", body)
        response = {"nonce": nonce, **result}
        response["hmac"] = hmac.new(
            key, _canonical({k: v for k, v in response.items() if k != "hmac"}), hashlib.sha256
        ).hexdigest()
        return _canonical(response)

    def _audit(self, operation: str, status: str, body: Mapping[str, Any]) -> None:
        policy = body.get("policy")
        job_id = policy.get("job_id") if isinstance(policy, Mapping) else body.get("job_id")
        record = {
            "timestamp": datetime.now(UTC).isoformat(),
            "operation": operation,
            "status": status,
            "installation_id": self.configuration.installation_id,
            "backend_instance_id": str(body.get("backend_instance_id") or ""),
            "user_id": str(body.get("user_id") or ""),
            "job_id": str(job_id or ""),
        }
        encoded = json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        try:
            with self._lock:
                self.configuration.audit_path.parent.mkdir(parents=True, exist_ok=True)
                with self.configuration.audit_path.open("a", encoding="ascii") as stream:
                    stream.write(encoded + "\n")
                    stream.flush()
                    os.fsync(stream.fileno())
        except OSError:
            logger.error("sandbox control-plane audit write failed")

    def _dispatch(self, operation: str, body: dict[str, Any]) -> dict[str, Any]:
        if operation == "status":
            return {
                "installed": bool(self.installed),
                "healthy": bool(self.installed and self._key is not None),
                "version": BROKER_VERSION,
                "installation_id": self.configuration.installation_id,
            }
        if operation in {"install", "repair"}:
            if self.adapter is None:
                raise SandboxInitializationError("Broker installation adapter is unavailable")
            getattr(self.adapter, operation)()
            self.installed = True
            return self._dispatch("status", {})
        if operation == "launch":
            if not self.installed or self.adapter is None:
                raise SandboxInitializationError("Broker is not ready to launch jobs")
            result = dict(self.adapter.launch(body))
            accepted = bool(result.get("accepted", False))
            if accepted:
                policy = body.get("policy")
                job_id = policy.get("job_id") if isinstance(policy, Mapping) else None
                backend_instance_id = body.get("backend_instance_id")
                user_id = body.get("user_id")
                if (
                    isinstance(job_id, str)
                    and job_id
                    and isinstance(backend_instance_id, str)
                    and backend_instance_id
                    and isinstance(user_id, str)
                    and user_id
                ):
                    resources = result.get("resources") if isinstance(result.get("resources"), Mapping) else {}
                    resources = dict(resources)
                    resources["owner_signature"] = self._resource_signature(
                        backend_instance_id,
                        user_id,
                        job_id,
                        resources,
                    )
                    self.manifest.register(
                        user_id,
                        job_id,
                        resources,
                        backend_instance_id=backend_instance_id,
                    )
                else:
                    raise SandboxInitializationError("Broker launch resource identity is invalid")
            return result
        if operation == "release":
            backend_instance_id = body.get("backend_instance_id")
            user_id = body.get("user_id")
            job_id = body.get("job_id")
            if (
                not isinstance(backend_instance_id, str)
                or not backend_instance_id
                or not isinstance(user_id, str)
                or not user_id
                or not isinstance(job_id, str)
                or not job_id
            ):
                raise SandboxInitializationError("Broker release job id is invalid")
            identity = (backend_instance_id, user_id, job_id)
            try:
                released = self.adapter is not None and bool(self.adapter.release(body))
            except Exception:
                released = False
            if not released:
                with self._lock:
                    self._pending_releases[identity] = dict(body)
                self._reclaimer.notify()
                raise SandboxCleanupPending("Broker Job resource cleanup is pending")
            with self._lock:
                self._pending_releases.pop(identity, None)
            self.manifest.remove(user_id, job_id, backend_instance_id=backend_instance_id)
            return {"released": True}
        if operation.startswith("process_"):
            if self.adapter is None:
                raise SandboxInitializationError("Broker process adapter is unavailable")
            return dict(self.adapter.control(operation, body))
        raise SandboxInitializationError("Broker operation is unsupported")

    def recover_orphans(
        self,
        live_job_ids: set[str],
        cleanup: Callable[[ResourceRecord], bool] | None = None,
    ) -> tuple[str, ...]:
        """Clean only records conclusively owned by this installation/backend."""

        removed: list[str] = []
        cleanup_record = cleanup
        if cleanup_record is None:
            if self.adapter is None:
                return ()
            cleanup_record = self.adapter.recover
        for record in self.manifest.records():
            if record.installation_id != self.configuration.installation_id or record.job_id in live_job_ids:
                continue
            signature = record.resources.get("owner_signature")
            if not isinstance(signature, str) or not hmac.compare_digest(
                signature,
                self._resource_signature(
                    record.backend_instance_id,
                    record.user_id,
                    record.job_id,
                    record.resources,
                ),
            ):
                logger.warning("sandbox resource ownership could not be verified for job %s", record.job_id)
                continue
            try:
                complete = bool(cleanup_record(record))
            except Exception:
                complete = False
            if complete:
                self.manifest.remove(
                    record.user_id,
                    record.job_id,
                    backend_instance_id=record.backend_instance_id,
                )
                removed.append(record.job_id)
        return tuple(removed)

    def _recover_pending_releases(self) -> tuple[str, ...]:
        if self.adapter is None:
            return ()
        with self._lock:
            pending = tuple(self._pending_releases.items())
        removed: list[str] = []
        for identity, body in pending:
            try:
                released = bool(self.adapter.release(body))
            except Exception:
                released = False
            if not released:
                continue
            backend_instance_id, user_id, job_id = identity
            self.manifest.remove(user_id, job_id, backend_instance_id=backend_instance_id)
            with self._lock:
                self._pending_releases.pop(identity, None)
            removed.append(job_id)
        return tuple(removed)

    def _resource_signature(
        self,
        backend_instance_id: str,
        user_id: str,
        job_id: str,
        resources: Mapping[str, object] | None = None,
    ) -> str:
        key = self._key or self.key_store.load()
        identity = {
            "installation_id": self.configuration.installation_id,
            "backend_instance_id": backend_instance_id,
            "user_id": user_id,
            "job_id": job_id,
            "resources": {str(name): value for name, value in (resources or {}).items() if name != "owner_signature"},
        }
        return hmac.new(key, _canonical(identity), hashlib.sha256).hexdigest()
