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
import os
import secrets
import subprocess
import tempfile
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Any, Protocol

from .errors import SandboxCleanupPending, SandboxInitializationError
from .manifest import ResourceManifest, ResourceRecord

BROKER_VERSION = "1"


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
        return cls(
            installation_id=installation_id or f"install-{uuid.uuid4().hex}",
            backend_instance_id=backend_instance_id or f"backend-{uuid.uuid4().hex}",
            program_data=Path(program_data or _default_program_data()),
            pipe_name=pipe_name,
        )

    @property
    def manifest_path(self) -> Path:
        return self.program_data / "resources.json"

    @property
    def installation_key_path(self) -> Path:
        return self.program_data / "installation.key.dpapi"


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
            return bytes(self._win32crypt.CryptProtectData(value, "Mini-Agent Sandbox Broker", None, None, None, 0)[1])
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


class WindowsServiceInstaller:
    """Install/repair the Broker as a virtual-account Windows service."""

    def __init__(
        self,
        service_command: tuple[str, ...],
        *,
        service_name: str = "MiniAgentSandboxBroker",
        runner: Callable[..., Any] | None = None,
        is_windows: bool | None = None,
    ) -> None:
        if not service_command or any(not isinstance(item, str) or not item for item in service_command):
            raise ValueError("service_command must contain non-empty strings")
        self.service_command = service_command
        self.service_name = service_name
        self.runner = runner or subprocess.run
        self.is_windows = os.name == "nt" if is_windows is None else is_windows

    def install(self) -> None:
        self._require_windows()
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
        self._run(["sc.exe", "start", self.service_name])

    def repair(self) -> None:
        self._require_windows()
        query = self.runner(["sc.exe", "query", self.service_name], check=False, capture_output=True)
        if getattr(query, "returncode", 1) != 0:
            self.install()
            return
        self._run(["sc.exe", "config", self.service_name, "start=", "demand"])
        self._run(["sc.exe", "start", self.service_name])

    def _run(self, command: list[str]) -> None:
        result = self.runner(command, check=False, capture_output=True)
        if getattr(result, "returncode", 1) != 0:
            raise SandboxInitializationError("Broker Windows service control failed")

    def _require_windows(self) -> None:
        if not self.is_windows:
            raise SandboxInitializationError("Windows Broker service is unavailable on this platform")


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
    ) -> None:
        self.configuration = configuration
        self.key_store = key_store or DpapiKeyStore(configuration.installation_key_path)
        self.adapter = adapter
        self.installed = installed
        self.is_windows = os.name == "nt" if is_windows is None else is_windows
        self.manifest = ResourceManifest(
            configuration.manifest_path,
            installation_id=configuration.installation_id,
            backend_instance_id=configuration.backend_instance_id,
        )
        self._key: bytes | None = None
        self._nonces: set[str] = set()
        self._lock = RLock()

    def initialize(self) -> None:
        if not self.is_windows:
            raise SandboxInitializationError("Windows Broker is unavailable on this platform")
        self._key = self.key_store.ensure()

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
        signature = request.get("hmac")
        body = request.get("body", {})
        if not isinstance(operation, str) or not operation or not isinstance(nonce, str) or not nonce:
            raise SandboxInitializationError("Broker request envelope is invalid")
        if not isinstance(signature, str) or not isinstance(body, Mapping):
            raise SandboxInitializationError("Broker request authentication is invalid")
        with self._lock:
            if nonce in self._nonces:
                raise SandboxInitializationError("Broker request replay detected")
            self._nonces.add(nonce)
        key = self._key or self.key_store.load()
        unsigned = {"operation": operation, "nonce": nonce, "body": dict(body)}
        expected = hmac.new(key, _canonical(unsigned), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise SandboxInitializationError("Broker request authentication failed")
        result = self._dispatch(operation, dict(body))
        response = {"nonce": nonce, **result}
        response["hmac"] = hmac.new(key, _canonical({k: v for k, v in response.items() if k != "hmac"}), hashlib.sha256).hexdigest()
        return _canonical(response)

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
                if isinstance(job_id, str) and job_id:
                    resources = result.get("resources") if isinstance(result.get("resources"), Mapping) else {}
                    self.manifest.register(job_id, resources)
            return result
        if operation == "release":
            job_id = body.get("job_id")
            if not isinstance(job_id, str) or not job_id:
                raise SandboxInitializationError("Broker release job id is invalid")
            self.manifest.remove(job_id)
            return {"released": True}
        raise SandboxInitializationError("Broker operation is unsupported")

    def recover_orphans(
        self,
        live_job_ids: set[str],
        cleanup: Callable[[ResourceRecord], bool],
    ) -> tuple[str, ...]:
        """Clean only records conclusively owned by this installation/backend."""

        removed: list[str] = []
        for record in self.manifest.owned_orphans(set(live_job_ids)):
            try:
                complete = bool(cleanup(record))
            except Exception:
                complete = False
            if complete:
                self.manifest.remove(record.job_id)
                removed.append(record.job_id)
        return tuple(removed)


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
        while not self._closed and not (stop is not None and stop()):
            self.serve_once()

    def close(self) -> None:
        self._closed = True

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
                1,
                1024 * 1024,
                1024 * 1024,
                0,
                self._security_attributes(),
            )
        except Exception as exc:  # pragma: no cover - Windows-only adapter
            raise SandboxInitializationError("Broker named-pipe creation failed") from exc

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
