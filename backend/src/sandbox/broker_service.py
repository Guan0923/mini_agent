"""Windows Broker service-side primitives.

The backend talks to the Broker through :class:`WindowsBrokerClient`; this
module contains the other side of that protocol and deliberately keeps all
privileged operations behind injected adapters.  The service can therefore be
tested on non-Windows without pretending that a normal Python process is a
security boundary.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import subprocess
import tempfile
import time
import uuid
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Any, Protocol

from .errors import SandboxCleanupPending, SandboxError, SandboxFailureCode, SandboxInitializationError
from .manifest import ResourceManifest, ResourceRecord
from .reclaimer import SandboxResourceReclaimer

BROKER_VERSION = "1"
MAX_REQUEST_TTL_SECONDS = 60
MAX_CLOCK_SKEW_SECONDS = 5
logger = logging.getLogger(__name__)


def _canonical(value: Mapping[str, Any]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def _default_program_data() -> Path:
    root = os.environ.get("PROGRAMDATA") if os.name == "nt" else None
    return Path(root or (Path(tempfile.gettempdir()) / "mini-agent-programdata")) / "Mini-Agent" / "SandboxBroker"


@dataclass(frozen=True, slots=True)
class BrokerConfiguration:
    """Installation-scoped Broker paths and identities."""

    installation_id: str
    backend_instance_id: str
    program_data: Path
    pipe_name: str = r"\\.\pipe\mini-agent-sandbox-broker"

    @classmethod
    def create(
        cls,
        *,
        program_data: Path | None = None,
        installation_id: str | None = None,
        backend_instance_id: str | None = None,
        pipe_name: str = r"\\.\pipe\mini-agent-sandbox-broker",
    ) -> BrokerConfiguration:
        resolved_program_data = Path(program_data or _default_program_data())
        if installation_id is None:
            try:
                persisted_id = (resolved_program_data / "installation.id").read_text(encoding="ascii").strip()
            except OSError:
                persisted_id = ""
            installation_id = persisted_id or f"install-{uuid.uuid4().hex}"
        return cls(
            installation_id=installation_id,
            backend_instance_id=backend_instance_id or f"backend-{uuid.uuid4().hex}",
            program_data=resolved_program_data,
            pipe_name=pipe_name,
        )

    @property
    def manifest_path(self) -> Path:
        return self.program_data / "resources.json"

    @property
    def installation_key_path(self) -> Path:
        return self.program_data / "installation.key.dpapi"

    @property
    def installation_id_path(self) -> Path:
        return self.program_data / "installation.id"

    @property
    def backend_sid_path(self) -> Path:
        return self.program_data / "backend.sid"

    @property
    def audit_path(self) -> Path:
        return self.program_data / "control-plane.jsonl"

    def persist_installation_id(self) -> None:
        self.program_data.mkdir(parents=True, exist_ok=True)
        try:
            existing = self.installation_id_path.read_text(encoding="ascii").strip()
        except OSError:
            existing = ""
        if existing:
            if existing != self.installation_id:
                raise SandboxInitializationError("Broker installation identity does not match ProgramData")
            return
        fd, temporary = tempfile.mkstemp(prefix=".installation.id.", dir=self.program_data)
        try:
            with os.fdopen(fd, "w", encoding="ascii") as stream:
                stream.write(self.installation_id)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.installation_id_path)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


class DpapiProvider(Protocol):
    def protect(self, value: bytes) -> bytes: ...

    def unprotect(self, value: bytes) -> bytes: ...


class WindowsDpapiProvider:
    """Thin pywin32 wrapper; importing this class is safe off Windows."""

    def __init__(self) -> None:
        if os.name != "nt":
            raise SandboxInitializationError("DPAPI is available only on Windows")
        try:
            import win32crypt  # type: ignore[import-not-found]
        except ImportError as exc:
            raise SandboxInitializationError("pywin32 is required for Broker DPAPI") from exc
        self._win32crypt = win32crypt

    def protect(self, value: bytes) -> bytes:
        try:
            return bytes(
                self._win32crypt.CryptProtectData(
                    value,
                    "Mini-Agent Sandbox Broker",
                    None,
                    None,
                    None,
                    0x4,
                )[1]
            )
        except Exception as exc:  # pragma: no cover - Windows-only adapter
            raise SandboxInitializationError("DPAPI could not protect the Broker key") from exc

    def unprotect(self, value: bytes) -> bytes:
        try:
            return bytes(self._win32crypt.CryptUnprotectData(value, None, None, None, 0)[1])
        except Exception as exc:  # pragma: no cover - Windows-only adapter
            raise SandboxInitializationError("DPAPI could not unprotect the Broker key") from exc


class DpapiKeyStore:
    """Atomically persist an installation key as DPAPI ciphertext."""

    def __init__(self, path: Path, *, provider: DpapiProvider | None = None) -> None:
        self.path = Path(path)
        self.provider = provider
        self._lock = RLock()

    def load(self) -> bytes:
        with self._lock:
            try:
                blob = self.path.read_bytes()
            except OSError as exc:
                raise SandboxInitializationError("Broker installation key is unavailable") from exc
        if not blob:
            raise SandboxInitializationError("Broker installation key is empty")
        provider = self._provider()
        key = provider.unprotect(blob)
        if len(key) < 32:
            raise SandboxInitializationError("Broker installation key is invalid")
        return key

    def ensure(self) -> bytes:
        with self._lock:
            if self.path.exists():
                return self.load()
            key = secrets.token_bytes(32)
            protected = self._provider().protect(key)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=self.path.parent)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(protected)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.path)
                try:
                    os.chmod(self.path, 0o600)
                except OSError:
                    pass
            finally:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass
            return key

    def _provider(self) -> DpapiProvider:
        if self.provider is not None:
            return self.provider
        return WindowsDpapiProvider()


class BrokerProcessAdapter(Protocol):
    def launch(self, request: Mapping[str, Any]) -> Mapping[str, Any]: ...

    def install(self) -> None: ...

    def repair(self) -> None: ...

    def control(self, operation: str, request: Mapping[str, Any]) -> Mapping[str, Any]: ...

    def release(self, request: Mapping[str, Any]) -> bool: ...

    def recover(self, record: ResourceRecord) -> bool: ...


class WindowsServiceInstaller:
    """Install/repair the Broker as a virtual-account Windows service."""

    def __init__(
        self,
        service_command: tuple[str, ...],
        *,
        service_name: str = "MiniAgentSandboxBroker",
        runner: Callable[..., Any] | None = None,
        is_windows: bool | None = None,
        backend_sid_path: Path | None = None,
        program_data_path: Path | None = None,
    ) -> None:
        if not service_command or any(not isinstance(item, str) or not item for item in service_command):
            raise ValueError("service_command must contain non-empty strings")
        self.service_command = service_command
        self.service_name = service_name
        self._runner_injected = runner is not None
        self.runner = runner or subprocess.run
        self.is_windows = os.name == "nt" if is_windows is None else is_windows
        self.backend_sid_path = Path(backend_sid_path) if backend_sid_path is not None else None
        self.program_data_path = Path(program_data_path) if program_data_path is not None else None

    def install(self) -> None:
        self._require_windows()
        self._write_backend_sid()
        command = subprocess.list2cmdline(list(self.service_command))
        self._run(
            [
                "sc.exe",
                "create",
                self.service_name,
                "type=",
                "own",
                "start=",
                "demand",
                "obj=",
                r"NT SERVICE\MiniAgentSandboxBroker",
                "binPath=",
                command,
            ]
        )
        self._run(["sc.exe", "sidtype", self.service_name, "unrestricted"])
        self._secure_program_data()
        self._run(["sc.exe", "start", self.service_name])

    def repair(self) -> None:
        self._require_windows()
        query = self.runner(["sc.exe", "query", self.service_name], check=False, capture_output=True)
        if getattr(query, "returncode", 1) != 0:
            self.install()
            return
        self._run(["sc.exe", "config", self.service_name, "start=", "demand"])
        self._secure_program_data()
        self._run(["sc.exe", "start", self.service_name])

    def _run(self, command: list[str]) -> None:
        if not self._runner_injected:
            self._run_elevated(command)
            return
        result = self.runner(command, check=False, capture_output=True)
        if getattr(result, "returncode", 1) != 0:
            raise SandboxInitializationError("Broker Windows service control failed")

    def _run_elevated(self, command: list[str]) -> None:
        """Run one control-plane command through the interactive UAC broker."""

        try:
            import win32api  # type: ignore[import-not-found]
            import win32con  # type: ignore[import-not-found]
            import win32process  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - Windows install path
            raise SandboxInitializationError("pywin32 is required for Broker UAC elevation") from exc
        try:  # pragma: no cover - requires an interactive Windows desktop
            result = win32api.ShellExecuteEx(
                fMask=getattr(win32con, "SEE_MASK_NOCLOSEPROCESS", 0x00000040),
                lpVerb="runas",
                lpFile=command[0],
                lpParameters=subprocess.list2cmdline(command[1:]),
                nShow=getattr(win32con, "SW_HIDE", 0),
            )
            handle = result["hProcess"] if isinstance(result, Mapping) else result
            win32process.GetExitCodeProcess(handle)
            import win32event  # type: ignore[import-not-found]

            win32event.WaitForSingleObject(handle, win32event.INFINITE)
            code = int(win32process.GetExitCodeProcess(handle))
            win32api.CloseHandle(handle)
        except Exception as exc:
            raise SandboxInitializationError("Broker Windows service control failed") from exc
        if code != 0:
            raise SandboxInitializationError("Broker Windows service control failed")

    def _secure_program_data(self) -> None:
        path = self.program_data_path
        sid_path = self.backend_sid_path
        if path is None or sid_path is None:
            return
        try:
            backend_sid = sid_path.read_text(encoding="ascii").strip()
        except OSError as exc:
            raise SandboxInitializationError("Broker backend SID is unavailable") from exc
        if not backend_sid or not path.is_absolute() or len(path.parts) < 3:
            raise SandboxInitializationError("Broker ProgramData path is invalid")
        # The path and SID are generated by the installer; quote them for the
        # command parser before handing the operation to elevated icacls.
        grants = [
            "SYSTEM:(OI)(CI)(F)",
            "Administrators:(OI)(CI)(F)",
            f"{backend_sid}:(OI)(CI)(M)",
            r"NT SERVICE\MiniAgentSandboxBroker:(OI)(CI)(M)",
        ]
        self._run(
            [
                "icacls.exe",
                str(path),
                "/inheritance:r",
                "/grant:r",
                *grants,
                "/T",
                "/C",
            ]
        )

    def _require_windows(self) -> None:
        if not self.is_windows:
            raise SandboxInitializationError("Windows Broker service is unavailable on this platform")

    def _write_backend_sid(self) -> None:
        if self.backend_sid_path is None:
            return
        try:
            import win32api  # type: ignore[import-not-found]
            import win32con  # type: ignore[import-not-found]
            import win32security  # type: ignore[import-not-found]

            token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
            sid = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
            value = win32security.ConvertSidToStringSid(sid)
            try:
                self.backend_sid_path.parent.mkdir(parents=True, exist_ok=True)
                self.backend_sid_path.write_text(value, encoding="ascii")
            except OSError:
                if self._runner_injected:
                    raise
                script = (
                    "$ErrorActionPreference='Stop'; "
                    f"New-Item -ItemType Directory -Force -LiteralPath {_powershell_quote(str(self.backend_sid_path.parent))} | Out-Null; "
                    f"Set-Content -LiteralPath {_powershell_quote(str(self.backend_sid_path))} "
                    f"-Value {_powershell_quote(value)} -Encoding ascii"
                )
                self._run_elevated(
                    [
                        "powershell.exe",
                        "-NoLogo",
                        "-NoProfile",
                        "-NonInteractive",
                        "-Command",
                        script,
                    ]
                )
        except Exception as exc:  # pragma: no cover - requires Windows install permissions
            raise SandboxInitializationError("Broker backend SID could not be persisted") from exc


def _powershell_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


@dataclass(frozen=True, slots=True)
class AccountLease:
    user_id: str
    job_id: str
    account: str
    sid: str
    kind: str


class AccountPool:
    """A bounded per-user account lease pool with cleanup-before-reuse."""

    def __init__(self, user_id: str, kind: str, accounts: tuple[tuple[str, str], ...]) -> None:
        if kind not in {"command", "mcp"}:
            raise ValueError("account pool kind must be command or mcp")
        if len(accounts) > 4:
            raise ValueError("an account pool may contain at most four accounts")
        self.user_id = user_id
        self.kind = kind
        self._accounts = tuple(accounts)
        self._active: dict[str, AccountLease] = {}
        self._lock = RLock()

    def acquire(self, job_id: str) -> AccountLease:
        if not isinstance(job_id, str) or not job_id:
            raise SandboxInitializationError("account lease Job id is invalid")
        with self._lock:
            if job_id in self._active:
                raise SandboxInitializationError("Job already owns an account lease")
            used = {lease.account for lease in self._active.values()}
            account = next(((name, sid) for name, sid in self._accounts if name not in used), None)
            if account is None:
                raise SandboxInitializationError("sandbox account pool is exhausted")
            lease = AccountLease(self.user_id, job_id, account[0], account[1], self.kind)
            self._active[job_id] = lease
            return lease

    def release(self, job_id: str, cleanup: Callable[[AccountLease], bool]) -> None:
        with self._lock:
            lease = self._active.get(job_id)
        if lease is None:
            return
        try:
            complete = bool(cleanup(lease))
        except Exception:
            complete = False
        if not complete:
            raise SandboxCleanupPending("sandbox account cleanup is pending")
        with self._lock:
            self._active.pop(job_id, None)

    def active(self) -> tuple[AccountLease, ...]:
        with self._lock:
            return tuple(self._active.values())


@dataclass(slots=True)
class UserAccountPools:
    command: AccountPool
    mcp: AccountPool

    @classmethod
    def create(
        cls,
        user_id: str,
        *,
        command_accounts: tuple[tuple[str, str], ...] = (),
        mcp_accounts: tuple[tuple[str, str], ...] = (),
    ) -> UserAccountPools:
        return cls(
            command=AccountPool(user_id, "command", command_accounts),
            mcp=AccountPool(user_id, "mcp", mcp_accounts),
        )


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
            from .native_broker_adapter import WindowsNativeBrokerAdapter
            from .native_windows import WindowsPowerShellWfpController

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


class WindowsNamedPipeServer:
    """Minimal synchronous named-pipe loop for the standalone service.

    The security descriptor is supplied by the service installer, so the
    runtime never widens an existing ACL.  The adapter is intentionally lazy:
    importing the module on Linux or before pywin32 installation remains safe,
    while attempting to serve without those capabilities fails closed.
    """

    def __init__(
        self,
        service: WindowsBrokerService,
        *,
        pipe_handle_factory: Callable[[], Any] | None = None,
        security_attributes_factory: Callable[[], Any] | None = None,
    ) -> None:
        self.service = service
        self.pipe_handle_factory = pipe_handle_factory
        self.security_attributes_factory = security_attributes_factory
        self._closed = False

    def serve_once(self) -> None:
        if os.name != "nt":
            raise SandboxInitializationError("Windows named pipes are unavailable on this platform")
        handle = self.pipe_handle_factory() if self.pipe_handle_factory is not None else self._create_pipe()
        try:
            import win32file  # type: ignore[import-not-found]
            import win32pipe  # type: ignore[import-not-found]

            win32pipe.ConnectNamedPipe(handle, None)
            _, payload = win32file.ReadFile(handle, 1024 * 1024)
            response = self.service.handle(payload)
            win32file.WriteFile(handle, response)
        except SandboxInitializationError:
            raise
        except Exception as exc:  # pragma: no cover - Windows-only adapter
            raise SandboxInitializationError("Broker named-pipe request failed") from exc
        finally:
            try:
                import win32file  # type: ignore[import-not-found]

                win32file.CloseHandle(handle)
            except Exception:
                pass

    def serve_forever(self, *, stop: Callable[[], bool] | None = None) -> None:
        workers = ThreadPoolExecutor(max_workers=16, thread_name_prefix="sandbox-broker-pipe")
        try:
            while not self._closed and not (stop is not None and stop()):
                handle = self.pipe_handle_factory() if self.pipe_handle_factory is not None else self._create_pipe()
                try:
                    import win32pipe  # type: ignore[import-not-found]

                    win32pipe.ConnectNamedPipe(handle, None)
                except Exception:
                    self._close_handle(handle)
                    raise
                workers.submit(self._serve_connected, handle)
        finally:
            workers.shutdown(wait=True, cancel_futures=True)

    def close(self) -> None:
        self._closed = True
        self.service.close()
        if os.name == "nt" and self.pipe_handle_factory is None:
            try:
                with open(self.service.configuration.pipe_name, "r+b", buffering=0):
                    pass
            except OSError:
                pass

    def _create_pipe(self) -> Any:
        try:
            import win32con  # type: ignore[import-not-found]
            import win32pipe  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - Windows-only adapter
            raise SandboxInitializationError("pywin32 is required for the Broker named pipe") from exc
        try:
            return win32pipe.CreateNamedPipe(
                self.service.configuration.pipe_name,
                win32con.PIPE_ACCESS_DUPLEX,
                win32pipe.PIPE_TYPE_MESSAGE | win32pipe.PIPE_READMODE_MESSAGE | win32pipe.PIPE_WAIT,
                win32pipe.PIPE_UNLIMITED_INSTANCES,
                1024 * 1024,
                1024 * 1024,
                0,
                self._security_attributes(),
            )
        except Exception as exc:  # pragma: no cover - Windows-only adapter
            raise SandboxInitializationError("Broker named-pipe creation failed") from exc

    def _serve_connected(self, handle: Any) -> None:
        try:
            import win32file  # type: ignore[import-not-found]

            _, payload = win32file.ReadFile(handle, 1024 * 1024)
            win32file.WriteFile(handle, self.service.handle(payload))
        except Exception:
            logger.warning("Broker named-pipe request failed", exc_info=False)
        finally:
            self._close_handle(handle)

    @staticmethod
    def _close_handle(handle: Any) -> None:
        try:
            import win32file  # type: ignore[import-not-found]

            win32file.CloseHandle(handle)
        except Exception:
            pass

    def _security_attributes(self) -> Any:
        if self.security_attributes_factory is None:
            raise SandboxInitializationError("Broker named-pipe ACL is not configured")
        try:
            return self.security_attributes_factory()
        except Exception as exc:  # pragma: no cover - Windows-only adapter
            raise SandboxInitializationError("Broker named-pipe ACL could not be created") from exc


__all__ = [
    "BROKER_VERSION",
    "BrokerConfiguration",
    "AccountLease",
    "AccountPool",
    "UserAccountPools",
    "DpapiKeyStore",
    "DpapiProvider",
    "WindowsServiceInstaller",
    "WindowsNamedPipeServer",
    "WindowsBrokerService",
    "WindowsDpapiProvider",
]
