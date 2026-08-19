"""Authenticated client for the standalone Windows Sandbox Broker."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from .errors import SandboxInitializationError


@dataclass(frozen=True, slots=True)
class BrokerStatus:
    installed: bool
    healthy: bool
    version: str | None = None
    installation_id: str | None = None
    detail: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "installed": self.installed,
            "healthy": self.healthy,
            "version": self.version,
            "installation_id": self.installation_id,
            "detail": self.detail,
        }


class WindowsBrokerClient:
    """Small protocol client; all privileged operations stay in the Broker."""

    def __init__(
        self,
        *,
        pipe_name: str = r"\\.\pipe\mini-agent-sandbox-broker",
        installation_key: bytes | None = None,
        transport: Callable[[bytes], bytes] | None = None,
        is_windows: bool | None = None,
        backend_instance_id: str | None = None,
        key_store: Any | None = None,
        installer: Any | None = None,
    ) -> None:
        self.pipe_name = pipe_name
        self._key = installation_key
        self._transport = transport
        self._is_windows = os.name == "nt" if is_windows is None else is_windows
        self.backend_instance_id = backend_instance_id or f"backend-{uuid.uuid4().hex}"
        self._key_store = key_store
        self._installer = installer
        self._seen_nonces: set[str] = set()

    @classmethod
    def from_system(cls, *, is_windows: bool | None = None) -> WindowsBrokerClient:
        """Load the locally installed DPAPI key without creating one.

        Installation is an explicit control-plane action.  A missing key is
        therefore represented by an unavailable client and never replaced by a
        plaintext or newly generated key during normal command execution.
        """

        resolved_windows = os.name == "nt" if is_windows is None else is_windows
        if not resolved_windows:
            return cls(is_windows=False)
        try:
            from .broker_service import BrokerConfiguration, DpapiKeyStore

            configuration = BrokerConfiguration.create()
            key_store = DpapiKeyStore(configuration.installation_key_path)
            key = key_store.load()
            return cls(
                pipe_name=configuration.pipe_name,
                installation_key=key,
                is_windows=True,
                key_store=key_store,
            )
        except Exception:
            try:
                key_store = DpapiKeyStore(configuration.installation_key_path)
            except Exception:
                key_store = None
            return cls(is_windows=True, key_store=key_store)

    @property
    def available(self) -> bool:
        return self._transport is not None or self._is_windows

    def status(self) -> BrokerStatus:
        if not self.available:
            return BrokerStatus(False, False, detail="Windows Broker is unavailable")
        try:
            payload = self.request("status", {})
            return BrokerStatus(
                bool(payload.get("installed")),
                bool(payload.get("healthy")),
                str(payload.get("version")) if payload.get("version") else None,
                str(payload.get("installation_id")) if payload.get("installation_id") else None,
                str(payload.get("detail")) if payload.get("detail") else None,
            )
        except Exception as exc:
            return BrokerStatus(False, False, detail=str(type(exc).__name__))

    def install(self) -> BrokerStatus:
        self._ensure_installation_key()
        return self._status_command("install")

    def repair(self) -> BrokerStatus:
        self._ensure_installation_key()
        return self._status_command("repair")

    def _ensure_installation_key(self) -> None:
        if self._key is not None:
            return
        if not self._is_windows or self._key_store is None:
            raise SandboxInitializationError("Broker installation key is unavailable")
        self._key = self._key_store.ensure()

    def _status_command(self, operation: str) -> BrokerStatus:
        if self._installer is not None:
            getattr(self._installer, operation)()
            payload = self.request("status", {})
        else:
            payload = self.request(operation, {})
        return BrokerStatus(bool(payload.get("installed", True)), bool(payload.get("healthy", True)))

    def launch(self, *, argv: list[str], cwd: str, environment: Mapping[str, str], policy: Mapping[str, Any]) -> Any:
        response = self.request(
            "launch", {"argv": list(argv), "cwd": cwd, "environment": dict(environment), "policy": dict(policy)}
        )
        if not response.get("accepted", False):
            raise SandboxInitializationError("Windows Broker rejected sandbox launch")
        # The real Broker returns a process handle/identifier. A development
        # client may return a Popen-compatible object for tests.
        process = response.get("process")
        if process is None:
            raise SandboxInitializationError("Windows Broker did not return a process handle")
        return process

    def release(self, job_id: str) -> None:
        """Drop the Broker-side resource lease for one completed Job."""

        response = self.request("release", {"job_id": job_id})
        if not response.get("released", False):
            raise SandboxInitializationError("Windows Broker did not release the Job")

    def request(self, operation: str, body: Mapping[str, Any]) -> dict[str, Any]:
        if not self.available:
            raise SandboxInitializationError("Windows Broker is not installed")
        nonce = secrets.token_urlsafe(24)
        if nonce in self._seen_nonces:
            raise SandboxInitializationError("Broker nonce collision")
        self._seen_nonces.add(nonce)
        envelope = {"operation": operation, "nonce": nonce, "body": dict(body)}
        encoded = _canonical(envelope)
        envelope["hmac"] = self._sign(encoded)
        response_bytes = self._send(_canonical(envelope))
        try:
            response = json.loads(response_bytes.decode("utf-8"))
        except (TypeError, ValueError) as exc:
            raise SandboxInitializationError("Windows Broker returned invalid data") from exc
        if not isinstance(response, dict) or response.get("nonce") != nonce:
            raise SandboxInitializationError("Windows Broker response replay detected")
        response_hmac = response.pop("hmac", None)
        if not isinstance(response_hmac, str) or not hmac.compare_digest(
            response_hmac, self._sign(_canonical(response))
        ):
            raise SandboxInitializationError("Windows Broker response authentication failed")
        return response

    def _send(self, payload: bytes) -> bytes:
        if self._transport is not None:
            return self._transport(payload)
        if not self._is_windows:
            raise SandboxInitializationError("Windows Broker is unavailable")
        try:
            with open(self.pipe_name, "r+b", buffering=0) as pipe:
                pipe.write(payload)
                return pipe.read(1024 * 1024)
        except OSError as exc:
            raise SandboxInitializationError("Windows Broker pipe is unavailable") from exc

    def _sign(self, payload: bytes) -> str:
        if self._key is None:
            raise SandboxInitializationError("Broker installation key is missing")
        return hmac.new(self._key, payload, hashlib.sha256).hexdigest()


def _canonical(value: Mapping[str, Any]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


__all__ = ["BrokerStatus", "WindowsBrokerClient"]
