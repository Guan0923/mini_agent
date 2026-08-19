from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path

import pytest

from backend.sandbox import (
    AggregateLimits,
    ApprovalDecision,
    ApprovalStore,
    BrokerConfiguration,
    DpapiKeyStore,
    NetworkMode,
    PermissionMode,
    ResourceMonitor,
    ResourceRequest,
    ResourceUsage,
    SandboxAdmission,
    SandboxAdmissionTimeout,
    SandboxInitializationError,
    SandboxLauncher,
    SandboxLimits,
    SandboxPolicy,
    SandboxResourceExceeded,
    WindowsBrokerClient,
    WindowsBrokerService,
    migrate_legacy_permission_mode,
    normalize_permission_mode,
)


def test_legacy_permission_modes_migrate_to_read_only() -> None:
    assert normalize_permission_mode("approval_for_me") is PermissionMode.READ_ONLY
    assert normalize_permission_mode("full_access") is PermissionMode.FULL_ACCESS
    assert normalize_permission_mode(None) is PermissionMode.READ_ONLY
    assert migrate_legacy_permission_mode("full_access") is PermissionMode.READ_ONLY


def test_limits_validate_hard_bounds() -> None:
    with pytest.raises(Exception):
        SandboxLimits(memory_mib=127).validate()
    assert SandboxLimits.from_mapping({"memory_mb": 128}).memory_mib == 128


def test_policy_requires_workspace_and_full_access_pair() -> None:
    workspace = Path.cwd()
    with pytest.raises(Exception):
        SandboxPolicy(
            workspace,
            "session",
            "job",
            file_mode=PermissionMode.FULL_ACCESS,
            enforced=False,
            full_access_acknowledged=True,
        )
    policy = SandboxPolicy(
        workspace,
        "session",
        "job",
        network_mode=NetworkMode.FULL_NETWORK,
        file_mode=PermissionMode.FULL_ACCESS,
        enforced=False,
        full_access_acknowledged=True,
    )
    assert len(policy.policy_hash()) == hashlib.sha256().digest_size * 2


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


def test_broker_request_authenticates_nonce() -> None:
    key = b"test-installation-key"

    def transport(payload: bytes) -> bytes:
        request = json.loads(payload)
        response = {"nonce": request["nonce"], "installed": True, "healthy": True}
        response["hmac"] = hmac.new(
            key,
            json.dumps(response, sort_keys=True, separators=(",", ":")).encode(),
            hashlib.sha256,
        ).hexdigest()
        return json.dumps(response, separators=(",", ":")).encode()

    client = WindowsBrokerClient(installation_key=key, transport=transport, is_windows=True)
    assert client.status().healthy


def test_broker_service_verifies_requests_and_recovers_owned_orphans(tmp_path: Path) -> None:
    class FakeDpapi:
        def protect(self, value: bytes) -> bytes:
            return b"protected:" + value

        def unprotect(self, value: bytes) -> bytes:
            assert value.startswith(b"protected:")
            return value.removeprefix(b"protected:")

    class Adapter:
        def install(self) -> None:
            return None

        def repair(self) -> None:
            return None

        def launch(self, request: dict[str, object]) -> dict[str, object]:
            return {"accepted": True, "resources": {"pid": 321}}

    configuration = BrokerConfiguration.create(program_data=tmp_path, installation_id="install-1", backend_instance_id="backend-1")
    store = DpapiKeyStore(configuration.installation_key_path, provider=FakeDpapi())
    service = WindowsBrokerService(configuration, key_store=store, adapter=Adapter(), is_windows=True)
    service.initialize()
    client = WindowsBrokerClient(installation_key=store.load(), transport=service.handle, is_windows=True)
    assert client.status().healthy
    response = service.handle(
        json.dumps(
            {
                "operation": "launch",
                "nonce": "nonce-launch",
                "body": {"policy": {"job_id": "job-1"}},
                "hmac": hmac.new(
                    store.load(),
                    json.dumps(
                        {"operation": "launch", "nonce": "nonce-launch", "body": {"policy": {"job_id": "job-1"}}},
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
    assert service.recover_orphans(set(), lambda record: record.job_id == "job-1") == ("job-1",)
    with pytest.raises(SandboxInitializationError):
        service.handle(response)


def test_resource_monitor_rejects_memory() -> None:
    monitor = ResourceMonitor(1, SandboxLimits(memory_mib=128), provider=lambda: None)  # type: ignore[arg-type]
    with pytest.raises(SandboxResourceExceeded):
        monitor.check(ResourceUsage(memory_bytes=129 * 1024 * 1024))


def test_policy_environment_does_not_inherit_profile_locations() -> None:
    policy = SandboxPolicy(Path.cwd(), "session", "job")
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


def test_windows_launcher_fails_closed_without_broker() -> None:
    policy = SandboxPolicy(Path.cwd(), "session", "job")
    launcher = SandboxLauncher(is_windows=True)
    with pytest.raises(SandboxInitializationError):
        launcher.launch(["cmd.exe", "/c", "echo ok"], policy)
