from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.sandbox import (
    FileAccessMode,
    NetworkMode,
    NetworkRule,
    SandboxInitializationError,
    SandboxLauncher,
    SandboxPathError,
    SandboxPathFailure,
    SandboxPolicy,
)
from backend.sandbox.broker_service import BrokerCredentialPackage
from backend.sandbox.native_broker_adapter import WindowsNativeBrokerAdapter
from backend.sandbox.native_broker_adapter.process import _process_creation_flags, _windows_command_line
from backend.sandbox.native_windows import AclLeaseEntry, WindowsAclManager, random_capability_sid
from backend.sandbox.native_windows.desktop import _station_participant_rights
from backend.sandbox.native_windows.wfp import build_static_filter_specs
from backend.sandbox.runtime.proxy import ProxyCredential, RunCommandProxy


class _Process:
    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.terminated = False

    def terminate(self) -> None:
        self.terminated = True


class _Acl:
    def __init__(self, calls: list[tuple]) -> None:
        self.calls = calls

    @staticmethod
    def _entry(path: Path, sid: str, ace_type: str = "allow") -> AclLeaseEntry:
        resolved = Path(path).resolve()
        return AclLeaseEntry(str(resolved), str(resolved).casefold(), sid, ace_type, 1, 3, True)

    def inspect_dacl(self, path: Path):
        resolved = Path(path).resolve(strict=True)
        return SimpleNamespace(path=resolved, object_id=str(resolved).casefold())

    def grant_lease(self, path: Path, sid: str, mode: FileAccessMode) -> AclLeaseEntry:
        self.calls.append(("grant", Path(path), sid, mode))
        return self._entry(path, sid)

    def grant_execute_lease(self, path: Path, sid: str) -> AclLeaseEntry:
        self.calls.append(("grant_execute", Path(path), sid))
        return self._entry(path, sid)

    def grant_capability_write(self, path: Path, sid: str) -> AclLeaseEntry:
        self.calls.append(("grant_capability", Path(path), sid))
        return self._entry(path, sid)

    def deny_capability_write(self, path: Path, sid: str) -> AclLeaseEntry:
        self.calls.append(("deny_capability", Path(path), sid))
        return self._entry(path, sid, "deny")

    def revoke_entry(self, entry: AclLeaseEntry) -> bool:
        self.calls.append(("revoke", Path(entry.path), entry.sid))
        return True

    @staticmethod
    def verify_entry(_entry: AclLeaseEntry) -> bool:
        return True


class _Proxy:
    def __init__(self, calls: list[tuple]) -> None:
        self.calls = calls

    def issue(self, job_id: str, rules: tuple[NetworkRule, ...], *, ttl_seconds: int) -> ProxyCredential:
        self.calls.append(("proxy_issue", job_id, rules, ttl_seconds))
        return ProxyCredential("job-user", "job-password")

    def revoke_job(self, job_id: str) -> None:
        self.calls.append(("proxy_revoke", job_id))


def test_static_wfp_policy_is_account_scoped_and_fail_closed() -> None:
    offline_sid = "S-1-5-21-1-2-3-1001"
    online_sid = "S-1-5-21-1-2-3-1002"
    specs = build_static_filter_specs(offline_sid, online_sid, 17831)

    assert len(specs) == 11
    assert len({spec.key for spec in specs}) == len(specs)
    outbound = [spec for spec in specs if "connect" in spec.name]
    assert {spec.user_sid for spec in outbound} == {offline_sid}
    permits = [spec for spec in outbound if "proxy" in spec.name]
    assert {spec.remote_port for spec in permits} == {17831}
    assert {spec.remote_address for spec in permits} == {"127.0.0.1"}
    assert all(spec.loopback_only and spec.tcp_only and spec.weight == 15 for spec in permits)
    assert all(spec.weight == 0 for spec in outbound if "block" in spec.name)
    inbound = [spec for spec in specs if "accept" in spec.name]
    assert {spec.user_sid for spec in inbound} == {offline_sid, online_sid}
    assert all(spec.loopback_only for spec in inbound if "loopback" in spec.name)
    assert all(spec.weight == 0 for spec in inbound if "block" in spec.name)


@pytest.mark.skipif(os.name != "nt", reason="Windows DACL integration test")
def test_real_windows_acl_lease_adds_verifies_and_removes_only_its_exact_ace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    manager = WindowsAclManager()
    sid = random_capability_sid()

    entry = manager.grant_capability_write(workspace, sid)

    assert entry.owned
    assert manager.verify_entry(entry)
    assert manager.revoke_entry(entry)
    assert not manager.verify_entry(entry)


def test_cmd_command_line_preserves_native_inner_quotes() -> None:
    value = _windows_command_line([r"C:\Windows\System32\cmd.exe", "/d", "/s", "/c", 'echo ok>"C:\\space dir\\x.txt"'])

    assert value == 'C:\\Windows\\System32\\cmd.exe /d /s /c "echo ok>"C:\\space dir\\x.txt""'
    assert r"\"" not in value


def test_restricted_process_does_not_use_hidden_console_mode() -> None:
    constants = SimpleNamespace(
        CREATE_SUSPENDED=0x4,
        CREATE_NO_WINDOW=0x08000000,
        CREATE_UNICODE_ENVIRONMENT=0x400,
    )

    flags = _process_creation_flags(constants)

    assert flags == constants.CREATE_SUSPENDED | constants.CREATE_UNICODE_ENVIRONMENT
    assert flags & constants.CREATE_NO_WINDOW == 0


def test_private_window_station_grants_logon_sid_read_control_without_acl_ownership() -> None:
    constants = SimpleNamespace(
        WINSTA_ACCESSCLIPBOARD=0x4,
        WINSTA_ACCESSGLOBALATOMS=0x20,
        WINSTA_CREATEDESKTOP=0x8,
        WINSTA_ENUMDESKTOPS=0x1,
        WINSTA_ENUMERATE=0x100,
        WINSTA_EXITWINDOWS=0x40,
        WINSTA_READATTRIBUTES=0x2,
        WINSTA_READSCREEN=0x200,
        WINSTA_WRITEATTRIBUTES=0x10,
        READ_CONTROL=0x20000,
        WRITE_DAC=0x40000,
        WRITE_OWNER=0x80000,
    )

    rights = _station_participant_rights(constants)

    assert rights & constants.READ_CONTROL
    assert rights & constants.WRITE_DAC == 0
    assert rights & constants.WRITE_OWNER == 0


@pytest.mark.parametrize(
    ("offline_sid", "online_sid", "proxy_port"),
    [
        ("not-a-sid", "S-1-5-21-1-2-3-1002", 17831),
        ("S-1-5-21-1-2-3-1001", "S-1-5-21-1-2-3-1001", 17831),
        ("S-1-5-21-1-2-3-1001", "S-1-5-21-1-2-3-1002", 0),
    ],
)
def test_static_wfp_policy_rejects_unsafe_identity_or_port(offline_sid: str, online_sid: str, proxy_port: int) -> None:
    with pytest.raises(ValueError):
        build_static_filter_specs(offline_sid, online_sid, proxy_port)


class _Broker:
    backend_instance_id = "backend-current"

    def __init__(self, calls: list[tuple]) -> None:
        self.calls = calls
        self.next_pid = 4000

    def reclaim_stale(self) -> tuple[str, ...]:
        self.calls.append(("reclaim",))
        return ()

    def reserve(self, *, policy, policy_hash: str, user_id: str):
        self.calls.append(("reserve", policy, policy_hash, user_id))
        return {
            "reserved": True,
            "reservation_id": f"reservation-{policy['job_id']}",
            "logon_sid": "S-1-5-5-1-2",
            "account_sid": "S-1-5-21-1-2-3-1001",
            "service_sid": "S-1-5-80-1-2-3-4-5",
            "capability_sids": {
                "workspace": "S-1-5-21-10-20-30-40",
                "temp": "S-1-5-21-50-60-70-80",
            },
            "capability_digest": "capability-digest",
        }

    def launch(self, *, environment, reservation_id: str, policy_hash: str, **kwargs):
        self.calls.append(("launch", dict(environment), reservation_id, policy_hash, kwargs))
        self.next_pid += 1
        return _Process(self.next_pid)

    def release(self, job_id: str, *, user_id: str) -> None:
        self.calls.append(("release", job_id, user_id))


@pytest.mark.parametrize("file_mode", list(FileAccessMode))
@pytest.mark.parametrize("network_mode", list(NetworkMode))
def test_launcher_uses_two_phase_broker_for_every_file_and_network_mode(
    tmp_path: Path,
    file_mode: FileAccessMode,
    network_mode: NetworkMode,
) -> None:
    calls: list[tuple] = []
    session_workspace = tmp_path / "session"
    project_workspace = tmp_path / "project"
    session_workspace.mkdir()
    project_workspace.mkdir()
    broker = _Broker(calls)
    proxy = _Proxy(calls)
    rules = (NetworkRule("127.0.0.1"),) if network_mode is NetworkMode.RESTRICTED_NETWORK else ()
    policy = SandboxPolicy(
        (session_workspace, project_workspace),
        "session",
        f"job-{file_mode.value}-{network_mode.value}",
        file_mode=file_mode,
        network_mode=network_mode,
        network_allowlist=rules,
    )
    launcher = SandboxLauncher(
        broker=broker,
        is_windows=True,
        acl_manager=_Acl(calls),
        lease_store_path=tmp_path / "leases.json",
        proxy_factory=lambda port: calls.append(("proxy", port)) or proxy,
    )

    process = launcher.launch(
        ["cmd.exe", "/c", "echo ok"],
        policy,
        cwd=project_workspace,
        user_id="local",
    )

    reserve = next(call for call in calls if call[0] == "reserve")
    launch = next(call for call in calls if call[0] == "launch")
    assert reserve[1]["file_mode"] == file_mode.value
    assert reserve[1]["network_mode"] == network_mode.value
    assert reserve[1]["workspaces"] == [str(session_workspace), str(project_workspace)]
    assert reserve[1]["cwd"] == str(project_workspace)
    granted_roots = {call[1] for call in calls if call[0] == "grant"}
    assert {session_workspace, project_workspace}.issubset(granted_roots)
    assert calls.index(reserve) < next(index for index, call in enumerate(calls) if call[0] == "grant")
    assert next(index for index, call in enumerate(calls) if call[0] == "grant") < calls.index(launch)
    if network_mode is NetworkMode.FULL_NETWORK:
        assert "HTTP_PROXY" not in launch[1]
    else:
        assert launch[1]["HTTP_PROXY"].startswith("http://")
        assert launch[1]["NO_PROXY"] == ""
    if network_mode is NetworkMode.RESTRICTED_NETWORK:
        assert any(call[0] == "proxy_issue" for call in calls)
    else:
        assert not any(call[0] == "proxy_issue" for call in calls)

    assert launcher.cleanup(process)
    assert any(call[0] == "release" for call in calls)
    revoked_roots = {call[1] for call in calls if call[0] == "revoke"}
    assert {session_workspace, project_workspace}.issubset(revoked_roots)


def test_workspace_and_cwd_failures_expose_stable_reason_and_absolute_path(tmp_path: Path) -> None:
    missing_workspace = tmp_path / "missing-workspace"
    with pytest.raises(SandboxPathError) as workspace_error:
        SandboxPolicy((missing_workspace,), "session", "job")
    assert workspace_error.value.reason is SandboxPathFailure.WORKSPACE_INVALID
    assert workspace_error.value.path == missing_workspace.absolute()

    calls: list[tuple] = []
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    missing_cwd = workspace / "missing-cwd"
    launcher = SandboxLauncher(
        broker=_Broker(calls),
        is_windows=True,
        acl_manager=_Acl(calls),
        lease_store_path=tmp_path / "leases.json",
    )
    with pytest.raises(SandboxPathError) as cwd_error:
        launcher.launch(
            ["cmd.exe", "/c", "echo ok"],
            SandboxPolicy((workspace,), "session", "job"),
            cwd=missing_cwd,
        )
    assert cwd_error.value.reason is SandboxPathFailure.CWD_INVALID
    assert cwd_error.value.path == missing_cwd.absolute()


def test_cwd_outside_workspace_is_rejected_with_final_absolute_path(tmp_path: Path) -> None:
    calls: list[tuple] = []
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    launcher = SandboxLauncher(
        broker=_Broker(calls),
        is_windows=True,
        acl_manager=_Acl(calls),
        lease_store_path=tmp_path / "leases.json",
    )

    with pytest.raises(SandboxPathError) as exc_info:
        launcher.launch(
            ["cmd.exe", "/c", "echo ok"],
            SandboxPolicy((workspace,), "session", "job"),
            cwd=outside,
        )

    assert exc_info.value.reason is SandboxPathFailure.CWD_OUTSIDE_WORKSPACE
    assert exc_info.value.path == outside.resolve()
    assert not any(call[0] == "reserve" for call in calls)


def test_workspace_descendant_link_is_not_scanned_but_linked_cwd_escape_is_rejected(tmp_path: Path) -> None:
    calls: list[tuple] = []
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    link = workspace / "node_modules-link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory links are unavailable: {exc}")
    launcher = SandboxLauncher(
        broker=_Broker(calls),
        is_windows=True,
        acl_manager=_Acl(calls),
        lease_store_path=tmp_path / "leases.json",
    )
    policy = SandboxPolicy((workspace,), "session", "job", network_mode=NetworkMode.FULL_NETWORK)

    process = launcher.launch(["cmd.exe", "/c", "echo ok"], policy, cwd=workspace)
    assert launcher.cleanup(process)
    with pytest.raises(SandboxPathError) as exc_info:
        launcher.launch(["cmd.exe", "/c", "echo ok"], policy, cwd=link)
    assert exc_info.value.reason is SandboxPathFailure.CWD_OUTSIDE_WORKSPACE
    assert exc_info.value.path == outside.resolve()


def test_dacl_read_verify_and_identity_failures_are_stable(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    class FailingReadAcl(_Acl):
        def inspect_dacl(self, path: Path):
            raise OSError(path)

    launcher = SandboxLauncher(
        broker=_Broker([]),
        is_windows=True,
        acl_manager=FailingReadAcl([]),
        lease_store_path=tmp_path / "read-leases.json",
    )
    with pytest.raises(SandboxPathError) as read_error:
        launcher.launch(["cmd.exe", "/c", "echo ok"], SandboxPolicy((workspace,), "session", "read"))
    assert read_error.value.reason is SandboxPathFailure.DACL_READ_FAILED
    assert read_error.value.path == workspace.resolve()

    class FailingVerifyAcl(_Acl):
        @staticmethod
        def verify_entry(_entry: AclLeaseEntry) -> bool:
            return False

    launcher = SandboxLauncher(
        broker=_Broker([]),
        is_windows=True,
        acl_manager=FailingVerifyAcl([]),
        lease_store_path=tmp_path / "verify-leases.json",
    )
    with pytest.raises(SandboxPathError) as verify_error:
        launcher.launch(["cmd.exe", "/c", "echo ok"], SandboxPolicy((workspace,), "session", "verify"))
    assert verify_error.value.reason is SandboxPathFailure.DACL_VERIFY_FAILED

    class ReplacedAcl(_Acl):
        def __init__(self, calls: list[tuple]) -> None:
            super().__init__(calls)
            self.workspace_reads = 0

        def inspect_dacl(self, path: Path):
            identity = super().inspect_dacl(path)
            if identity.path == workspace.resolve():
                self.workspace_reads += 1
                if self.workspace_reads >= 4:
                    return SimpleNamespace(path=identity.path, object_id="replacement")
            return identity

    launcher = SandboxLauncher(
        broker=_Broker([]),
        is_windows=True,
        acl_manager=ReplacedAcl([]),
        lease_store_path=tmp_path / "identity-leases.json",
    )
    with pytest.raises(SandboxPathError) as identity_error:
        launcher.launch(["cmd.exe", "/c", "echo ok"], SandboxPolicy((workspace,), "session", "identity"))
    assert identity_error.value.reason is SandboxPathFailure.PATH_IDENTITY_CHANGED
    assert identity_error.value.path == workspace.resolve()


def test_proxy_authenticates_pins_dns_and_allows_explicit_loopback() -> None:
    targets: list[socket.socket] = []
    target_threads: list[threading.Thread] = []
    for _ in range(2):
        target = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        target.bind(("127.0.0.1", 0))
        target.listen(1)
        targets.append(target)
        thread = threading.Thread(target=_serve_http_once, args=(target,), daemon=True)
        thread.start()
        target_threads.append(thread)
    target_ports = [int(target.getsockname()[1]) for target in targets]
    proxy_port = _free_port()
    resolutions: list[tuple[str, int]] = []

    def resolver(host: str, port: int, **_kwargs):
        resolutions.append((host, port))
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", port))]

    proxy = RunCommandProxy(proxy_port, resolver=resolver)
    proxy.start()
    credential = proxy.issue("job-1", (NetworkRule("allowed.test"),), ttl_seconds=30)
    authorization = base64.b64encode(f"{credential.username}:{credential.password}".encode()).decode()
    try:
        for target_port in target_ports:
            with socket.create_connection(("127.0.0.1", proxy_port), timeout=2) as client:
                client.sendall(
                    (
                        f"GET http://allowed.test:{target_port}/ok HTTP/1.1\r\n"
                        f"Host: allowed.test:{target_port}\r\n"
                        f"Proxy-Authorization: Basic {authorization}\r\n\r\n"
                    ).encode()
                )
                response = _receive_all(client)
            assert b"200 OK" in response
            assert b"proxy-ok" in response
        assert resolutions == [("allowed.test", port) for port in target_ports]

        with socket.create_connection(("127.0.0.1", proxy_port), timeout=2) as client:
            client.sendall(
                (
                    f"GET http://denied.test:{target_ports[0]}/ HTTP/1.1\r\n"
                    f"Host: denied.test:{target_ports[0]}\r\n"
                    f"Proxy-Authorization: Basic {authorization}\r\n\r\n"
                ).encode()
            )
            assert b"403" in _receive_all(client)

        with socket.create_connection(("127.0.0.1", proxy_port), timeout=2) as client:
            client.sendall(
                f"GET http://allowed.test:{target_ports[0]}/ HTTP/1.1\r\nHost: allowed.test\r\n\r\n".encode()
            )
            assert b"407" in _receive_all(client)
    finally:
        proxy.close()
        for target in targets:
            target.close()
        for thread in target_threads:
            thread.join(timeout=2)


@pytest.mark.parametrize(
    ("configured", "candidate"),
    [
        ("EXAMPLE.TEST.", "example.test"),
        ("localhost", "LOCALHOST."),
        ("127.0.0.1", "127.0.0.1"),
        ("0:0:0:0:0:0:0:1", "::1"),
        ("10.0.0.8", "10.0.0.8"),
        ("192.168.1.20", "192.168.1.20"),
    ],
)
def test_proxy_matches_exact_host_rules_for_public_and_local_targets(configured: str, candidate: str) -> None:
    assert RunCommandProxy._allowed((NetworkRule(configured),), candidate, 443)


def test_proxy_does_not_extend_a_rule_to_subdomains_or_aliases() -> None:
    rules = (NetworkRule("example.test"), NetworkRule("127.0.0.1"))

    assert not RunCommandProxy._allowed(rules, "sub.example.test", 443)
    assert not RunCommandProxy._allowed(rules, "localhost", 443)


def test_proxy_rule_with_port_does_not_allow_other_ports() -> None:
    rule = NetworkRule("example.test", 443)

    assert RunCommandProxy._allowed((rule,), "example.test", 443)
    assert not RunCommandProxy._allowed((rule,), "example.test", 80)


def test_broker_reservation_rejects_hash_tampering_and_expiry(tmp_path: Path) -> None:
    now = [10.0]
    handles: list[_Handle] = []

    class TokenFactory:
        def reserve(self, account, file_mode):
            handle = _Handle()
            handles.append(handle)
            return SimpleNamespace(
                token=handle,
                logon_sid="S-1-5-5-10-20",
                account_sid=account.sid,
                workspace_cap_sid="S-1-5-21-10-20-30-40",
                temp_cap_sid="S-1-5-21-50-60-70-80",
            )

    class Desktop:
        startup_name = r"Winsta0\MiniAgentTest"

        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    package = BrokerCredentialPackage(
        "generation-1",
        "CodexSandboxOffline",
        "S-1-5-21-1-2-3-1001",
        "offline-password",
        "CodexSandboxOnline",
        "S-1-5-21-1-2-3-1002",
        "online-password",
    )
    adapter = WindowsNativeBrokerAdapter(
        credentials=package,
        service_sid="S-1-5-80-1-2-3-4-5",
        token_factory=TokenFactory(),
        desktop_factory=lambda *_args: Desktop(),
        clock=lambda: now[0],
    )
    policy = {
        **SandboxPolicy((tmp_path,), "session", "job", network_mode=NetworkMode.NO_NETWORK).to_dict(),
        "cwd": str(tmp_path),
        "temp_dir": str(tmp_path),
    }
    policy_hash = _hash(policy)
    try:
        with pytest.raises(SandboxInitializationError, match="hash"):
            adapter.reserve(
                {
                    "policy": policy,
                    "policy_hash": "0" * 64,
                    "backend_instance_id": "backend",
                    "user_id": "local",
                }
            )
        reservation = adapter.reserve(
            {
                "policy": policy,
                "policy_hash": policy_hash,
                "backend_instance_id": "backend",
                "user_id": "local",
            }
        )
        now[0] = 41.0
        with pytest.raises(SandboxInitializationError, match="unavailable"):
            adapter.launch(
                {
                    "reservation_id": reservation["reservation_id"],
                    "policy_hash": policy_hash,
                    "capability_digest": reservation["capability_digest"],
                    "backend_instance_id": "backend",
                    "user_id": "local",
                    "argv": ["cmd.exe"],
                    "environment": {},
                    "cwd": str(tmp_path),
                }
            )
        assert handles[-1].closed
    finally:
        adapter.close()


class _Handle:
    def __init__(self) -> None:
        self.closed = False

    def Close(self) -> None:
        self.closed = True


def _hash(value: dict[str, object]) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return hashlib.sha256(encoded).hexdigest()


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as current:
        current.bind(("127.0.0.1", 0))
        return int(current.getsockname()[1])


def _serve_http_once(listener: socket.socket) -> None:
    client, _ = listener.accept()
    with client:
        client.recv(65536)
        body = b"proxy-ok"
        client.sendall(b"HTTP/1.1 200 OK\r\nConnection: close\r\nContent-Length: 8\r\n\r\n" + body)


def _receive_all(client: socket.socket) -> bytes:
    chunks: list[bytes] = []
    while True:
        value = client.recv(65536)
        if not value:
            return b"".join(chunks)
        chunks.append(value)
