from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import threading
import types
from pathlib import Path

import pytest

from backend.sandbox import (
    AggregateLimits,
    ApprovalDecision,
    ApprovalStore,
    BrokerConfiguration,
    BrokerManagedProcess,
    DpapiKeyStore,
    FileAccessMode,
    NetworkMode,
    NetworkRule,
    PermissionMode,
    ResourceLimits,
    ResourceMonitor,
    ResourceRequest,
    ResourceUsage,
    SandboxAdmission,
    SandboxAdmissionTimeout,
    SandboxInitializationError,
    SandboxJobContext,
    SandboxLauncher,
    SandboxLimits,
    SandboxPolicy,
    SandboxResourceExceeded,
    WindowsBrokerClient,
    WindowsBrokerService,
    WindowsDpapiProvider,
    WindowsNamedPipeServer,
    normalize_permission_mode,
    resolve_network_rules,
)
from backend.sandbox.broker_service import BrokerCredentialPackage, build_ready_marker
from backend.sandbox.runtime.leases import CommandLease


def test_permission_modes_use_only_the_three_level_contract(tmp_path: Path) -> None:
    assert {mode.value for mode in PermissionMode} == {"read_only", "workspace_write", "full_access"}
    assert normalize_permission_mode("full_access") is PermissionMode.FULL_ACCESS
    assert normalize_permission_mode(None) is PermissionMode.READ_ONLY
    assert PermissionMode is FileAccessMode
    assert SandboxLimits is ResourceLimits
    assert SandboxJobContext("user-1", SandboxPolicy((tmp_path,), "session", "job")).job_kind == "command"


def test_limits_validate_hard_bounds() -> None:
    with pytest.raises(Exception):
        SandboxLimits(memory_mib=127).validate()
    assert SandboxLimits.from_mapping({"memory_mb": 128}).memory_mib == 128


@pytest.mark.parametrize("network_mode", list(NetworkMode))
def test_full_access_file_mode_is_independent_from_network_mode(tmp_path: Path, network_mode: NetworkMode) -> None:
    allowlist = (NetworkRule("example.test"),) if network_mode is NetworkMode.RESTRICTED_NETWORK else ()
    policy = SandboxPolicy(
        (tmp_path,),
        "session",
        "job",
        network_mode=network_mode,
        network_allowlist=allowlist,
        file_mode=PermissionMode.FULL_ACCESS,
    )
    assert len(policy.policy_hash()) == hashlib.sha256().digest_size * 2


def test_restricted_network_rejects_non_public_resolution() -> None:
    def resolver(host: str, port: int, **kwargs: object) -> list[tuple[object, ...]]:
        del host, kwargs
        return [(2, 1, 6, "", ("127.0.0.1", port))]

    with pytest.raises(Exception, match="non-public"):
        resolve_network_rules((NetworkRule("localhost", 80),), resolver=resolver)


def test_authorization_grant_stores_only_hash() -> None:
    store = ApprovalStore()
    grant = store.decide(
        session_id="session-1",
        command="echo secret",
        cwd="C:\\workspace",
        permission_target="workspace_write",
        decision=ApprovalDecision.ALLOW_SESSION,
    )
    assert grant is not None
    assert "echo secret" not in json.dumps(grant.to_public())
    assert store.allowed(
        session_id="session-1",
        command="echo secret",
        cwd="C:\\workspace",
        permission_target="workspace_write",
    )


def test_authorization_grant_uses_local_repository_after_restart() -> None:
    class Repository:
        def __init__(self) -> None:
            self.values: set[tuple[str, str, str]] = set()

        def save_sandbox_approval(self, session_id, request_hash, command_hash, cwd_hash, permission_target, *rest):
            assert command_hash != "echo secret"
            assert cwd_hash != "C:\\workspace"
            assert "echo secret" not in json.dumps(rest)
            self.values.add((session_id, request_hash, permission_target))

        def has_sandbox_approval(self, session_id, request_hash, permission_target):
            return (session_id, request_hash, permission_target) in self.values

    repository = Repository()
    ApprovalStore(repository).decide(
        session_id="session-1",
        command="echo secret",
        cwd="C:\\workspace",
        permission_target="workspace_write",
        decision="allow_session",
    )
    assert ApprovalStore(repository).allowed(
        session_id="session-1",
        command="echo secret",
        cwd="C:\\workspace",
        permission_target="workspace_write",
    )


def test_broker_request_authenticates_nonce() -> None:
    key = b"test-installation-key"

    def transport(payload: bytes) -> bytes:
        request = json.loads(payload)
        response = {
            "nonce": request["nonce"],
            "installed": True,
            "healthy": True,
            "version": "2",
            "generation": "test-generation",
            "proxy_port": 17831,
            "token_model": "capability_sid_v1",
        }
        response["hmac"] = hmac.new(
            key,
            json.dumps(response, sort_keys=True, separators=(",", ":")).encode(),
            hashlib.sha256,
        ).hexdigest()
        return json.dumps(response, separators=(",", ":")).encode()

    client = WindowsBrokerClient(installation_key=key, transport=transport, is_windows=True)
    assert client.status().healthy


def test_broker_managed_process_proxies_communicate() -> None:
    key = b"proxy-installation-key"

    def transport(payload: bytes) -> bytes:
        request = json.loads(payload)
        operation = request["operation"]
        if operation == "launch":
            values = {
                "accepted": True,
                "process_id": "process-1",
                "pid": 4321,
                "stdin": "null",
                "stdout": "pipe",
                "stderr": "pipe",
            }
        elif operation == "process_communicate":
            values = {"returncode": 0, "stdout": "b2s=", "stderr": ""}
        else:
            raise AssertionError(operation)
        response = {"nonce": request["nonce"], **values}
        response["hmac"] = hmac.new(
            key,
            json.dumps(response, sort_keys=True, separators=(",", ":")).encode(),
            hashlib.sha256,
        ).hexdigest()
        return json.dumps(response, separators=(",", ":")).encode()

    client = WindowsBrokerClient(installation_key=key, transport=transport, is_windows=True)
    process = client.launch(
        argv=["cmd.exe", "/c", "echo ok"],
        cwd="C:\\workspace",
        environment={},
        reservation_id="reservation-1",
        policy_hash="policy-hash",
        capability_digest="capability-digest",
        user_id="user-1",
    )
    assert isinstance(process, BrokerManagedProcess)
    assert process.communicate(timeout=1) == (b"ok", b"")


def test_dpapi_key_store_repairs_an_empty_placeholder(tmp_path: Path) -> None:
    class FakeDpapi:
        def protect(self, value: bytes) -> bytes:
            return b"protected:" + value

        def unprotect(self, value: bytes) -> bytes:
            return value.removeprefix(b"protected:")

    path = tmp_path / "installation.key.dpapi"
    path.write_bytes(b"")
    store = DpapiKeyStore(path, provider=FakeDpapi())

    key = store.ensure()

    assert len(key) == 32
    assert path.read_bytes() == b"protected:" + key
    assert store.load() == key


@pytest.mark.skipif(os.name != "nt", reason="Windows DPAPI test")
def test_windows_dpapi_provider_accepts_pywin32_bytes_results(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeWin32Crypt:
        @staticmethod
        def CryptProtectData(value, *args):
            return b"protected:" + value

        @staticmethod
        def CryptUnprotectData(value, *args):
            return value.removeprefix(b"protected:")

    monkeypatch.setitem(sys.modules, "win32crypt", FakeWin32Crypt)
    provider = WindowsDpapiProvider()

    protected = provider.protect(b"key")

    assert protected == b"protected:key"
    assert provider.unprotect(protected) == b"key"


def test_named_pipe_close_releases_a_blocked_listener(monkeypatch: pytest.MonkeyPatch) -> None:
    connected = threading.Event()
    released = threading.Event()
    handle = object()

    class Service:
        configuration = types.SimpleNamespace(pipe_name=r"\\.\pipe\test")
        closed = False

        def close(self) -> None:
            self.closed = True

    service = Service()

    def connect_named_pipe(current, overlapped) -> None:
        assert current is handle
        assert overlapped is None
        connected.set()
        released.wait(timeout=2)
        raise OSError("listener closed")

    monkeypatch.setitem(sys.modules, "win32pipe", types.SimpleNamespace(ConnectNamedPipe=connect_named_pipe))
    monkeypatch.setitem(
        sys.modules,
        "win32file",
        types.SimpleNamespace(CloseHandle=lambda current: released.set() if current is handle else None),
    )
    server = WindowsNamedPipeServer(service, pipe_handle_factory=lambda: handle)
    worker = threading.Thread(target=server.serve_forever)
    worker.start()
    assert connected.wait(timeout=1)

    server.close()
    worker.join(timeout=2)

    assert not worker.is_alive()
    assert service.closed is True


def test_broker_service_verifies_requests_and_recovers_owned_orphans(tmp_path: Path) -> None:
    class FakeDpapi:
        def protect(self, value: bytes) -> bytes:
            return b"protected:" + value

        def unprotect(self, value: bytes) -> bytes:
            assert value.startswith(b"protected:")
            return value.removeprefix(b"protected:")

    class Adapter:
        def launch(self, request: dict[str, object]) -> dict[str, object]:
            return {
                "accepted": True,
                "backend_instance_id": "backend-1",
                "user_id": "user-1",
                "job_id": "job-1",
                "resources": {"pid": 321},
            }

    package = BrokerCredentialPackage(
        "generation-1",
        "CodexSandboxOffline",
        "S-1-5-21-1-2-3-1001",
        "offline-password",
        "CodexSandboxOnline",
        "S-1-5-21-1-2-3-1002",
        "online-password",
    )
    credential_store = types.SimpleNamespace(load=lambda: package)

    configuration = BrokerConfiguration.create(
        program_data=tmp_path,
        installation_id="install-1",
        backend_instance_id="backend-1",
    )
    store = DpapiKeyStore(configuration.installation_key_path, provider=FakeDpapi())
    store.ensure()
    service = WindowsBrokerService(
        configuration,
        key_store=store,
        adapter=Adapter(),
        is_windows=True,
        clock=lambda: 1_000,
        credential_store=credential_store,
        ready_reader=lambda _path: build_ready_marker(package, 17831),
    )
    service.initialize()
    client = WindowsBrokerClient(
        installation_key=store.load(),
        transport=service.handle,
        is_windows=True,
        backend_instance_id="backend-1",
        clock=lambda: 1_000,
    )
    assert client.status().healthy
    response = service.handle(
        json.dumps(
            {
                "operation": "launch",
                "nonce": "nonce-launch",
                "issued_at": 1_000,
                "expires_at": 1_030,
                "body": {
                    "backend_instance_id": "backend-1",
                    "user_id": "user-1",
                    "policy": {"job_id": "job-1"},
                },
                "hmac": hmac.new(
                    store.load(),
                    json.dumps(
                        {
                            "operation": "launch",
                            "nonce": "nonce-launch",
                            "issued_at": 1_000,
                            "expires_at": 1_030,
                            "body": {
                                "backend_instance_id": "backend-1",
                                "user_id": "user-1",
                                "policy": {"job_id": "job-1"},
                            },
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode(),
                    hashlib.sha256,
                ).hexdigest(),
            },
            separators=(",", ":"),
        ).encode()
    )
    assert json.loads(response)["nonce"] == "nonce-launch"
    assert service.manifest.records()[0].job_id == "job-1"
    assert service.manifest.records()[0].user_id == "user-1"
    assert service.recover_orphans(set(), lambda record: record.job_id == "job-1") == ("job-1",)
    with pytest.raises(SandboxInitializationError):
        service.handle(response)


def test_broker_service_rejects_expired_signed_request(tmp_path: Path) -> None:
    key = b"k" * 32

    class KeyStore:
        def ensure(self):
            return key

        def load(self):
            return key

    configuration = BrokerConfiguration.create(program_data=tmp_path)
    package = BrokerCredentialPackage(
        "generation-1",
        "CodexSandboxOffline",
        "S-1-5-21-1-2-3-1001",
        "offline-password",
        "CodexSandboxOnline",
        "S-1-5-21-1-2-3-1002",
        "online-password",
    )
    service = WindowsBrokerService(
        configuration,
        key_store=KeyStore(),
        adapter=types.SimpleNamespace(),
        is_windows=True,
        clock=lambda: 100,
        credential_store=types.SimpleNamespace(load=lambda: package),
        ready_reader=lambda _path: build_ready_marker(package, 17831),
    )
    service.initialize()
    unsigned = {
        "operation": "status",
        "nonce": "expired",
        "issued_at": 1,
        "expires_at": 2,
        "body": {},
    }
    request = {
        **unsigned,
        "hmac": hmac.new(
            key, json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode(), hashlib.sha256
        ).hexdigest(),
    }
    with pytest.raises(SandboxInitializationError, match="expired"):
        service.handle(json.dumps(request).encode())


def test_resource_monitor_rejects_memory() -> None:
    monitor = ResourceMonitor(1, SandboxLimits(memory_mib=128), provider=lambda: None)  # type: ignore[arg-type]
    with pytest.raises(SandboxResourceExceeded):
        monitor.check(ResourceUsage(memory_bytes=129 * 1024 * 1024))


def test_resource_monitor_fails_closed_when_sampling_breaks() -> None:
    exceeded = threading.Event()

    class Provider:
        def sample(self, _pid: int) -> ResourceUsage:
            raise OSError("accounting unavailable")

    monitor = ResourceMonitor(1, SandboxLimits(), provider=Provider(), on_exceeded=lambda _error: exceeded.set())
    monitor.start()
    try:
        assert exceeded.wait(1.0)
    finally:
        monitor.stop()


def test_policy_environment_does_not_inherit_profile_locations(tmp_path: Path) -> None:
    policy = SandboxPolicy((tmp_path,), "session", "job")
    environment = policy.environment(
        {
            "PATH": "C:\\Windows",
            "USERPROFILE": "C:\\Users\\real",
            "HOMEDRIVE": "C:",
            "HOMEPATH": "\\Users\\real",
            "BACKEND_API_TOKEN": "secret",
        },
        temp_dir=Path("C:\\sandbox-tmp\\job"),
    )
    assert environment["TEMP"] == "C:\\sandbox-tmp\\job"
    assert environment["USERPROFILE"] == environment["TEMP"]
    assert environment["HOME"] == environment["TEMP"]
    assert environment.get("HOMEPATH") != "\\Users\\real"
    assert "BACKEND_API_TOKEN" not in environment


def test_sandbox_admission_times_out_and_releases() -> None:
    admission = SandboxAdmission(
        user_limits=AggregateLimits(memory_mib=128, processes=1, handles=64, cpu_percent=75),
        system_limits=AggregateLimits(memory_mib=128, processes=1, handles=64, cpu_percent=90),
        wait_seconds=0.01,
    )
    request = ResourceRequest(memory_mib=128, processes=1, handles=64)
    admission.acquire("session", request)
    with pytest.raises(SandboxAdmissionTimeout):
        admission.acquire("session", request)
    admission.release("session", request)
    admission.acquire("session", request)


def test_windows_launcher_fails_closed_without_broker(tmp_path: Path) -> None:
    policy = SandboxPolicy((tmp_path,), "session", "job")
    launcher = SandboxLauncher(is_windows=True)
    with pytest.raises(SandboxInitializationError):
        launcher.launch(["cmd.exe", "/c", "echo ok"], policy)


def test_launcher_releases_broker_before_removing_temp_dir(tmp_path: Path) -> None:
    calls: list[str] = []

    class Broker:
        def __init__(self) -> None:
            self.temp_dir: Path | None = None

        def release(self, _job_id: str, *, user_id: str) -> None:
            del user_id
            assert self.temp_dir is not None and self.temp_dir.exists()
            calls.append("broker")

    class Acl:
        def revoke_lease(self, _path: Path, _sid: str) -> bool:
            calls.append("acl")
            return True

    broker = Broker()
    policy = SandboxPolicy((tmp_path,), "session", "job")
    temp_dir = tmp_path / "scratch" / "job"
    temp_dir.mkdir(parents=True)
    broker.temp_dir = temp_dir
    launcher = SandboxLauncher(
        broker=broker,
        is_windows=True,
        allow_local_backend=True,
        acl_manager=Acl(),
        lease_store_path=tmp_path / "leases.json",
    )
    launcher._temp_dirs[1234] = temp_dir
    launcher._job_contexts[1234] = SandboxJobContext("user-1", policy)
    lease = CommandLease(
        "job",
        "reservation",
        "S-1-5-5-1-2",
        "S-1-5-21-1-2-3-1001",
        "S-1-5-80-1-2-3-4-5",
        (str(tmp_path),),
        str(temp_dir),
        "read_only",
        "S-1-5-21-10-20-30-40",
        "S-1-5-21-50-60-70-80",
        "capability-digest",
        (),
        {},
    )
    launcher.lease_store.add(lease)
    launcher._leases[1234] = lease

    assert launcher.cleanup(1234)
    assert not temp_dir.exists()
    assert calls[0] == "broker"


def test_launcher_terminates_through_broker_managed_process() -> None:
    class Process:
        terminated = False

        def terminate(self) -> None:
            self.terminated = True

    launcher = SandboxLauncher(is_windows=True, allow_local_backend=True)
    process = Process()

    launcher.terminate_tree(process)

    assert process.terminated
