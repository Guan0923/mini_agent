"""Authenticated client for the standalone Windows Sandbox Broker."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import subprocess
import sys
import time
import uuid
from base64 import b64decode, b64encode
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..errors import (
    BrokerInstallationError,
    BrokerInstallFailureCode,
    BrokerStatusFailureCode,
    SandboxCleanupPending,
    SandboxError,
    SandboxFailureCode,
    SandboxInitializationError,
    SandboxPolicyError,
    SandboxResourceExceeded,
)

_PROCESS_BACKEND_INSTANCE_ID = f"backend-{uuid.uuid4().hex}"


def _broker_status_failure_code(detail: str) -> BrokerStatusFailureCode:
    exact_codes = {
        "Windows Broker is unavailable": BrokerStatusFailureCode.UNAVAILABLE,
        "Windows Broker is not installed": BrokerStatusFailureCode.NOT_INSTALLED,
        "Broker service configuration requires repair": BrokerStatusFailureCode.SERVICE_CONFIGURATION_INVALID,
        "Broker ready marker is unavailable": BrokerStatusFailureCode.READY_MARKER_UNAVAILABLE,
        "Broker proxy port requires repair": BrokerStatusFailureCode.PROXY_CONFIGURATION_INVALID,
        "Broker installation key is missing": BrokerStatusFailureCode.INSTALLATION_KEY_MISSING,
        "Broker installation key is unavailable": BrokerStatusFailureCode.INSTALLATION_KEY_MISSING,
        "Windows Broker pipe is unavailable": BrokerStatusFailureCode.PIPE_UNAVAILABLE,
        "Broker protocol version requires repair": BrokerStatusFailureCode.PROTOCOL_INCOMPATIBLE,
        "Broker token model requires repair": BrokerStatusFailureCode.TOKEN_MODEL_INCOMPATIBLE,
        "Broker generation requires repair": BrokerStatusFailureCode.GENERATION_MISMATCH,
    }
    if code := exact_codes.get(detail):
        return code
    if detail.startswith("Broker ready marker ") or detail == "Broker credential generation is invalid":
        return BrokerStatusFailureCode.READY_MARKER_INVALID
    if detail in {
        "Windows Broker returned invalid data",
        "Windows Broker response replay detected",
        "Windows Broker returned an invalid error",
        "Windows Broker returned an unknown error",
    }:
        return BrokerStatusFailureCode.RESPONSE_INVALID
    if detail == "Windows Broker response authentication failed":
        return BrokerStatusFailureCode.RESPONSE_AUTHENTICATION_FAILED
    return BrokerStatusFailureCode.STATUS_FAILED


@dataclass(frozen=True, slots=True)
class BrokerStatus:
    installed: bool
    healthy: bool
    code: BrokerStatusFailureCode | None = None
    version: str | None = None
    installation_id: str | None = None
    detail: str | None = None
    generation: str | None = None
    proxy_port: int | None = None
    token_model: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "installed": self.installed,
            "healthy": self.healthy,
            "code": self.code.value if self.code is not None else None,
            "version": self.version,
            "installation_id": self.installation_id,
            "detail": self.detail,
            "generation": self.generation,
            "proxy_port": self.proxy_port,
            "token_model": self.token_model,
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
        clock: Callable[[], float] | None = None,
        request_ttl_seconds: int = 30,
        ready_path: Path | None = None,
        expected_proxy_port: int = 17831,
    ) -> None:
        self.pipe_name = pipe_name
        self._key = installation_key
        self._transport = transport
        self._is_windows = os.name == "nt" if is_windows is None else is_windows
        self.backend_instance_id = backend_instance_id or _PROCESS_BACKEND_INSTANCE_ID
        self._key_store = key_store
        self._installer = installer
        self._clock = clock or time.time
        if not 1 <= request_ttl_seconds <= 60:
            raise ValueError("request_ttl_seconds must be between 1 and 60")
        self._request_ttl_seconds = request_ttl_seconds
        self._seen_nonces: set[str] = set()
        self._ready_path = Path(ready_path) if ready_path is not None else None
        self._expected_proxy_port = expected_proxy_port

    @classmethod
    def from_system(
        cls,
        *,
        is_windows: bool | None = None,
        expected_proxy_port: int = 17831,
    ) -> WindowsBrokerClient:
        """Load the locally installed DPAPI key without creating one.

        Installation is an explicit control-plane action.  A missing key is
        therefore represented by an unavailable client and never replaced by a
        plaintext or newly generated key during normal command execution.
        """

        resolved_windows = os.name == "nt" if is_windows is None else is_windows
        if not resolved_windows:
            return cls(is_windows=False)
        try:
            from ..broker_service import BrokerConfiguration, DpapiKeyStore, WindowsServiceInstaller
        except Exception:
            return cls(is_windows=True)
        configuration = BrokerConfiguration.create()
        key_store = DpapiKeyStore(configuration.installation_key_path)
        source_root = Path(__file__).resolve().parents[2]
        installer = WindowsServiceInstaller(
            (str(Path(sys.prefix) / "pythonservice.exe"),),
            service_class=(rf"{source_root}\sandbox_service_bootstrap.MiniAgentSandboxBrokerService"),
            backend_sid_path=configuration.backend_sid_path,
            program_data_path=configuration.program_data,
            service_code_path=source_root,
            service_code_boundary_path=Path(__file__).resolve().parents[4],
            service_runtime_paths=(Path(sys.prefix).resolve(), Path(sys.base_prefix).resolve()),
            proxy_port=expected_proxy_port,
        )
        try:
            key = key_store.load()
        except Exception:
            key = None
        return cls(
            pipe_name=configuration.pipe_name,
            installation_key=key,
            is_windows=True,
            key_store=key_store,
            installer=installer,
            ready_path=configuration.ready_path,
            expected_proxy_port=expected_proxy_port,
        )

    @property
    def available(self) -> bool:
        return self._transport is not None or self._is_windows

    def status(self) -> BrokerStatus:
        if not self.available:
            return BrokerStatus(
                False,
                False,
                code=BrokerStatusFailureCode.UNAVAILABLE,
                detail="Windows Broker is unavailable",
            )
        installed = True
        try:
            service_installed = getattr(self._installer, "service_installed", None)
            if callable(service_installed):
                installed = bool(service_installed())
                if not installed:
                    return BrokerStatus(
                        False,
                        False,
                        code=BrokerStatusFailureCode.NOT_INSTALLED,
                        detail="Windows Broker is not installed",
                    )
            configuration_healthy = getattr(self._installer, "configuration_healthy", None)
            if callable(configuration_healthy) and not configuration_healthy():
                raise SandboxInitializationError("Broker service configuration requires repair")
            if self._ready_path is not None:
                from ..broker_service import read_ready_marker

                marker = read_ready_marker(self._ready_path, expected_proxy_port=self._expected_proxy_port)
            else:
                marker = {}
            payload = self.request("status", {})
            if payload.get("version") != "3":
                raise SandboxInitializationError("Broker protocol version requires repair")
            if payload.get("token_model") != "capability_sid_v3":
                raise SandboxInitializationError("Broker token model requires repair")
            if marker and payload.get("generation") != marker.get("generation"):
                raise SandboxInitializationError("Broker generation requires repair")
            healthy = bool(payload.get("healthy"))
            payload_installed = bool(payload.get("installed"))
            resolved_installed = installed if self._installer is not None else payload_installed
            detail = str(payload.get("detail")) if payload.get("detail") else None
            return BrokerStatus(
                resolved_installed,
                healthy,
                code=None if healthy else BrokerStatusFailureCode.UNHEALTHY,
                version=str(payload.get("version")) if payload.get("version") else None,
                installation_id=(str(payload.get("installation_id")) if payload.get("installation_id") else None),
                detail=None if healthy else detail or "Broker reported unhealthy status",
                generation=str(payload.get("generation")) if payload.get("generation") else None,
                proxy_port=int(payload["proxy_port"]) if isinstance(payload.get("proxy_port"), int) else None,
                token_model=str(payload.get("token_model")) if payload.get("token_model") else None,
            )
        except Exception as exc:
            detail = str(exc) or type(exc).__name__
            return BrokerStatus(
                installed,
                False,
                code=_broker_status_failure_code(detail),
                detail=detail,
            )

    def install(self) -> BrokerStatus:
        current = self.status()
        if current.healthy:
            return current
        if self._installer is None:
            self._ensure_installation_key()
        return self._status_command("repair" if self._installer is not None else "install")

    def repair(self) -> BrokerStatus:
        current = self.status()
        if current.healthy:
            return current
        if self._installer is None:
            self._ensure_installation_key()
        return self._status_command("repair")

    def reinstall(self) -> BrokerStatus:
        """Force a full elevated Broker replacement even when status is healthy."""

        if self._installer is None or not callable(getattr(self._installer, "reinstall", None)):
            raise SandboxInitializationError("Windows Broker reinstall is unavailable")
        self._installer.reinstall()
        self._key = None
        deadline = time.monotonic() + 10.0
        last_error: Exception | None = None
        while True:
            try:
                if self._key_store is None:
                    raise SandboxInitializationError("Broker installation key is unavailable")
                self._key = self._key_store.load()
                status = self.status()
                if status.healthy:
                    return status
                raise SandboxInitializationError(status.detail or "Broker is not healthy")
            except Exception as exc:
                last_error = exc
                self._key = None
                if time.monotonic() >= deadline:
                    raise BrokerInstallationError(
                        BrokerInstallFailureCode.NOT_READY,
                        "Broker 服务已重装但未能在限定时间内就绪。",
                    ) from last_error
                time.sleep(0.1)

    def _ensure_installation_key(self) -> None:
        if self._key is not None:
            return
        if not self._is_windows or self._key_store is None:
            raise SandboxInitializationError("Broker installation key is unavailable")
        self._key = self._key_store.ensure()

    def _status_command(self, operation: str) -> BrokerStatus:
        if self._installer is not None:
            getattr(self._installer, operation)()
            deadline = time.monotonic() + 10.0
            last_error: Exception | None = None
            while True:
                try:
                    if self._key is None and self._key_store is not None:
                        self._key = self._key_store.load()
                    status = self.status()
                    if status.healthy:
                        return status
                    raise SandboxInitializationError(status.detail or "Broker is not healthy")
                except Exception as exc:
                    last_error = exc
                    if time.monotonic() >= deadline:
                        raise BrokerInstallationError(
                            BrokerInstallFailureCode.NOT_READY,
                            "Broker 服务已安装但未能在限定时间内就绪。",
                        ) from last_error
                    time.sleep(0.1)
        payload = self.request(operation, {})
        healthy = bool(payload.get("healthy", True))
        return BrokerStatus(
            bool(payload.get("installed", True)),
            healthy,
            code=None if healthy else BrokerStatusFailureCode.UNHEALTHY,
            detail=(
                str(payload.get("detail"))
                if not healthy and payload.get("detail")
                else ("Broker reported unhealthy status" if not healthy else None)
            ),
        )

    def launch(
        self,
        *,
        argv: list[str],
        cwd: str,
        environment: Mapping[str, str],
        reservation_id: str,
        policy_hash: str,
        capability_digest: str,
        user_id: str,
    ) -> Any:
        response = self.request(
            "launch",
            {
                "argv": list(argv),
                "cwd": cwd,
                "environment": dict(environment),
                "reservation_id": reservation_id,
                "policy_hash": policy_hash,
                "capability_digest": capability_digest,
                "backend_instance_id": self.backend_instance_id,
                "user_id": user_id,
            },
        )
        if not response.get("accepted", False):
            raise SandboxInitializationError("Windows Broker rejected sandbox launch")
        # The real Broker returns a process handle/identifier. A development
        # client may return a Popen-compatible object for tests.
        process = response.get("process")
        if process is not None:
            return process
        process_id = response.get("process_id")
        pid = response.get("pid")
        if not isinstance(process_id, str) or not process_id or isinstance(pid, bool) or not isinstance(pid, int):
            raise SandboxInitializationError("Windows Broker did not return a process handle")
        return BrokerManagedProcess(
            self,
            process_id,
            pid,
            stdin_enabled=response.get("stdin") == "pipe",
            stdout_enabled=response.get("stdout") == "pipe",
            stderr_enabled=response.get("stderr") == "pipe",
        )

    def reserve(self, *, policy: Mapping[str, Any], policy_hash: str, user_id: str) -> dict[str, Any]:
        response = self.request(
            "reserve",
            {
                "policy": dict(policy),
                "policy_hash": policy_hash,
                "backend_instance_id": self.backend_instance_id,
                "user_id": user_id,
            },
        )
        reservation_id = response.get("reservation_id")
        logon_sid = response.get("logon_sid")
        capability_sids = response.get("capability_sids")
        capability_digest = response.get("capability_digest")
        if (
            not response.get("reserved")
            or not isinstance(reservation_id, str)
            or not isinstance(logon_sid, str)
            or not isinstance(capability_sids, Mapping)
            or not isinstance(capability_sids.get("workspace"), str)
            or not isinstance(capability_sids.get("temp"), str)
            or not isinstance(capability_digest, str)
            or not capability_digest
        ):
            raise SandboxInitializationError("Windows Broker did not return a reservation")
        return response

    def reclaim_stale(self) -> tuple[str, ...]:
        response = self.request("reclaim", {"backend_instance_id": self.backend_instance_id})
        raw = response.get("reclaimed")
        if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
            raise SandboxInitializationError("Windows Broker returned invalid reclaim data")
        return tuple(raw)

    def release(self, job_id: str, *, user_id: str) -> None:
        """Drop the Broker-side resource lease for one completed Job."""

        response = self.request(
            "release",
            {"backend_instance_id": self.backend_instance_id, "user_id": user_id, "job_id": job_id},
        )
        if not response.get("released", False):
            raise SandboxInitializationError("Windows Broker did not release the Job")

    def request(self, operation: str, body: Mapping[str, Any]) -> dict[str, Any]:
        if not self.available:
            raise SandboxInitializationError("Windows Broker is not installed")
        nonce = secrets.token_urlsafe(24)
        if nonce in self._seen_nonces:
            raise SandboxInitializationError("Broker nonce collision")
        self._seen_nonces.add(nonce)
        issued_at = int(self._clock())
        envelope = {
            "operation": operation,
            "nonce": nonce,
            "issued_at": issued_at,
            "expires_at": issued_at + self._request_ttl_seconds,
            "body": dict(body),
        }
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
        error = response.get("error")
        if error is not None:
            self._raise_remote_error(error)
        return response

    @staticmethod
    def _raise_remote_error(value: object) -> None:
        if not isinstance(value, Mapping):
            raise SandboxInitializationError("Windows Broker returned an invalid error")
        raw_code = value.get("code")
        try:
            code = SandboxFailureCode(str(raw_code))
        except ValueError as exc:
            raise SandboxInitializationError("Windows Broker returned an unknown error") from exc
        message = "Windows Broker operation failed"
        errors: dict[SandboxFailureCode, type[SandboxError]] = {
            SandboxFailureCode.INIT_FAILED: SandboxInitializationError,
            SandboxFailureCode.POLICY_FAILED: SandboxPolicyError,
            SandboxFailureCode.RESOURCE_EXCEEDED: SandboxResourceExceeded,
            SandboxFailureCode.CLEANUP_PENDING: SandboxCleanupPending,
        }
        error_type = errors.get(code)
        if error_type is not None:
            raise error_type(message)
        raise SandboxError(message, code)

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


class _BrokerReadStream:
    def __init__(self, process: BrokerManagedProcess, stream: str) -> None:
        self._process = process
        self._stream = stream

    def read(self, size: int = -1) -> bytes:
        response = self._process._control(
            "process_read",
            {"stream": self._stream, "size": 65536 if size is None or size < 0 else size},
        )
        payload = response.get("data")
        if not isinstance(payload, str):
            raise OSError("Broker process stream returned invalid data")
        return b64decode(payload.encode("ascii"))

    def close(self) -> None:
        return None


class _BrokerWriteStream:
    def __init__(self, process: BrokerManagedProcess) -> None:
        self._process = process

    def write(self, value: bytes) -> int:
        response = self._process._control(
            "process_write",
            {"data": b64encode(bytes(value)).decode("ascii")},
        )
        return int(response.get("written", len(value)))

    def flush(self) -> None:
        return None

    def close(self) -> None:
        self._process._control("process_close_stdin", {})


class BrokerManagedProcess:
    """Popen-compatible proxy for a process owned by the Broker service."""

    def __init__(
        self,
        client: WindowsBrokerClient,
        process_id: str,
        pid: int,
        *,
        stdin_enabled: bool,
        stdout_enabled: bool,
        stderr_enabled: bool,
    ) -> None:
        self._client = client
        self._process_id = process_id
        self.pid = pid
        self.returncode: int | None = None
        self.stdin = _BrokerWriteStream(self) if stdin_enabled else None
        self.stdout = _BrokerReadStream(self, "stdout") if stdout_enabled else None
        self.stderr = _BrokerReadStream(self, "stderr") if stderr_enabled else None

    def _control(self, operation: str, values: Mapping[str, Any]) -> dict[str, Any]:
        return self._client.request(
            operation,
            {
                "process_id": self._process_id,
                "backend_instance_id": self._client.backend_instance_id,
                **dict(values),
            },
        )

    def poll(self) -> int | None:
        response = self._control("process_poll", {})
        return self._capture_returncode(response)

    def wait(self, timeout: float | None = None) -> int:
        response = self._control("process_wait", {"timeout": timeout})
        code = self._capture_returncode(response)
        if code is None:
            raise subprocess.TimeoutExpired(["sandbox-process"], timeout)
        return code

    def communicate(
        self, input: bytes | None = None, timeout: float | None = None
    ) -> tuple[bytes | None, bytes | None]:
        response = self._control(
            "process_communicate",
            {
                "input": b64encode(input).decode("ascii") if input is not None else None,
                "timeout": timeout,
            },
        )
        code = self._capture_returncode(response)
        if code is None:
            raise subprocess.TimeoutExpired(["sandbox-process"], timeout)
        stdout = response.get("stdout")
        stderr = response.get("stderr")
        return (
            b64decode(stdout.encode("ascii")) if isinstance(stdout, str) else None,
            b64decode(stderr.encode("ascii")) if isinstance(stderr, str) else None,
        )

    def terminate(self) -> None:
        self._capture_returncode(self._control("process_terminate", {}))

    def kill(self) -> None:
        self._capture_returncode(self._control("process_kill", {}))

    def _capture_returncode(self, response: Mapping[str, Any]) -> int | None:
        value = response.get("returncode")
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int):
            raise OSError("Broker process returned an invalid exit code")
        self.returncode = value
        return value


def _canonical(value: Mapping[str, Any]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


__all__ = ["BrokerManagedProcess", "BrokerStatus", "WindowsBrokerClient"]
