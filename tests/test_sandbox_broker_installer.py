from __future__ import annotations

import hashlib
import hmac
import json
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest
from fastapi import Request

from backend.api.routes.sandbox import install as install_broker
from backend.api.routes.sandbox import reinstall as reinstall_broker
from backend.api.routes.sandbox import repair as repair_broker
from backend.api.routes.sandbox import status as status_broker
from backend.sandbox import (
    BrokerStatusFailureCode,
    SandboxInitializationError,
    SandboxMaintenanceBusy,
    SandboxMaintenanceGate,
    WindowsBrokerClient,
)
from backend.sandbox.broker_service import (
    BrokerCredentialPackage,
    WindowsServiceInstaller,
    build_ready_marker,
)
from backend.sandbox.broker_service.installer import _elevated_helper_argv, _write_python_service_path
from backend.sandbox.errors import BrokerInstallationError, BrokerInstallFailureCode
from backend.sandbox.install_helper import (
    EXIT_ACCOUNT_FAILED,
    EXIT_SERVICE_STOP_FAILED,
    _icacls_sid,
    _persist_sid,
    _remove_owned_accounts,
    _runtime_acl_grants,
    _secure_program_data,
    _service_sid,
    _source_acl_grants,
    _stop_service_for_repair,
    _TransactionFailure,
    run_transaction,
)


class _Result:
    def __init__(self, returncode: int = 0) -> None:
        self.returncode = returncode


class _StatusInstaller:
    def __init__(self, *, installed: bool = True, configuration_healthy: bool = True) -> None:
        self.installed = installed
        self.healthy = configuration_healthy

    def service_installed(self) -> bool:
        return self.installed

    def configuration_healthy(self) -> bool:
        return self.healthy


def _status_transport(key: bytes, **overrides: object):
    def transport(payload: bytes) -> bytes:
        request = json.loads(payload)
        response = {
            "nonce": request["nonce"],
            "installed": True,
            "healthy": True,
            "version": "3",
            "generation": "generation-1",
            "proxy_port": 17831,
            "token_model": "capability_sid_v3",
            **overrides,
        }
        response["hmac"] = hmac.new(
            key,
            json.dumps(response, sort_keys=True, separators=(",", ":")).encode(),
            hashlib.sha256,
        ).hexdigest()
        return json.dumps(response, separators=(",", ":")).encode()

    return transport


def test_broker_status_reports_only_confirmed_missing_service_as_not_installed() -> None:
    status = WindowsBrokerClient(is_windows=True, installer=_StatusInstaller(installed=False)).status()

    assert status.installed is False
    assert status.healthy is False
    assert status.code is BrokerStatusFailureCode.NOT_INSTALLED
    assert status.detail == "Windows Broker is not installed"


def test_reinstall_reloads_the_new_installation_key() -> None:
    old_key = b"o" * 32
    new_key = b"n" * 32
    calls: list[str] = []

    class Installer:
        def reinstall(self) -> None:
            calls.append("reinstall")

        def service_installed(self) -> bool:
            return True

        def configuration_healthy(self) -> bool:
            return True

    class KeyStore:
        def load(self) -> bytes:
            calls.append("load")
            return new_key

    client = WindowsBrokerClient(
        installation_key=old_key,
        transport=_status_transport(new_key),
        is_windows=True,
        installer=Installer(),
        key_store=KeyStore(),
    )

    status = client.reinstall()

    assert status.healthy is True
    assert calls == ["reinstall", "load"]


def test_broker_status_preserves_safe_initialization_detail_for_installed_service() -> None:
    status = WindowsBrokerClient(is_windows=True, installer=_StatusInstaller()).status()

    assert status.installed is True
    assert status.healthy is False
    assert status.code is BrokerStatusFailureCode.INSTALLATION_KEY_MISSING
    assert status.detail == "Broker installation key is missing"


def test_broker_status_distinguishes_invalid_service_configuration() -> None:
    status = WindowsBrokerClient(
        is_windows=True,
        installer=_StatusInstaller(configuration_healthy=False),
    ).status()

    assert status.installed is True
    assert status.code is BrokerStatusFailureCode.SERVICE_CONFIGURATION_INVALID
    assert status.detail == "Broker service configuration requires repair"


@pytest.mark.parametrize(
    ("marker", "expected_code", "expected_detail"),
    [
        (None, BrokerStatusFailureCode.READY_MARKER_UNAVAILABLE, "Broker ready marker is unavailable"),
        ({"invalid": True}, BrokerStatusFailureCode.READY_MARKER_INVALID, "Broker ready marker is invalid"),
    ],
)
def test_broker_status_distinguishes_ready_marker_failures(
    tmp_path: Path,
    marker: dict[str, object] | None,
    expected_code: BrokerStatusFailureCode,
    expected_detail: str,
) -> None:
    ready_path = tmp_path / "ready.json"
    if marker is not None:
        ready_path.write_text(json.dumps(marker), encoding="utf-8")
    status = WindowsBrokerClient(
        installation_key=b"k" * 32,
        transport=_status_transport(b"k" * 32),
        is_windows=True,
        installer=_StatusInstaller(),
        ready_path=ready_path,
    ).status()

    assert status.installed is True
    assert status.code is expected_code
    assert status.detail == expected_detail


@pytest.mark.parametrize(
    ("proxy_port", "generation", "expected_code", "expected_detail"),
    [
        (
            17832,
            "generation-1",
            BrokerStatusFailureCode.PROXY_CONFIGURATION_INVALID,
            "Broker proxy port requires repair",
        ),
        (
            17831,
            "generation-2",
            BrokerStatusFailureCode.GENERATION_MISMATCH,
            "Broker generation requires repair",
        ),
    ],
)
def test_broker_status_distinguishes_marker_configuration_mismatches(
    tmp_path: Path,
    proxy_port: int,
    generation: str,
    expected_code: BrokerStatusFailureCode,
    expected_detail: str,
) -> None:
    key = b"k" * 32
    package = BrokerCredentialPackage(
        "generation-1",
        "SandboxOffline",
        "S-1-5-21-1-2-3-1001",
        "offline-password",
        "SandboxOnline",
        "S-1-5-21-1-2-3-1002",
        "online-password",
    )
    ready_path = tmp_path / "ready.json"
    ready_path.write_text(json.dumps(build_ready_marker(package, proxy_port)), encoding="utf-8")
    status = WindowsBrokerClient(
        installation_key=key,
        transport=_status_transport(key, generation=generation),
        is_windows=True,
        installer=_StatusInstaller(),
        ready_path=ready_path,
    ).status()

    assert status.installed is True
    assert status.code is expected_code
    assert status.detail == expected_detail


@pytest.mark.parametrize(
    ("overrides", "expected_code", "expected_detail"),
    [
        (
            {"version": "1"},
            BrokerStatusFailureCode.PROTOCOL_INCOMPATIBLE,
            "Broker protocol version requires repair",
        ),
        (
            {"token_model": "legacy"},
            BrokerStatusFailureCode.TOKEN_MODEL_INCOMPATIBLE,
            "Broker token model requires repair",
        ),
        (
            {"healthy": False, "detail": "Broker service stopped"},
            BrokerStatusFailureCode.UNHEALTHY,
            "Broker service stopped",
        ),
    ],
)
def test_broker_status_classifies_protocol_and_health_failures(
    overrides: dict[str, object],
    expected_code: BrokerStatusFailureCode,
    expected_detail: str,
) -> None:
    key = b"k" * 32
    status = WindowsBrokerClient(
        installation_key=key,
        transport=_status_transport(key, **overrides),
        is_windows=True,
        installer=_StatusInstaller(),
    ).status()

    assert status.installed is True
    assert status.code is expected_code
    assert status.detail == expected_detail


@pytest.mark.parametrize(
    ("failure", "expected_code"),
    [
        (SandboxInitializationError("Windows Broker pipe is unavailable"), BrokerStatusFailureCode.PIPE_UNAVAILABLE),
        (RuntimeError("  exact raw status failure\n"), BrokerStatusFailureCode.STATUS_FAILED),
    ],
)
def test_broker_status_preserves_complete_transport_failure(
    failure: Exception,
    expected_code: BrokerStatusFailureCode,
) -> None:
    def transport(payload: bytes) -> bytes:
        del payload
        raise failure

    status = WindowsBrokerClient(
        installation_key=b"k" * 32,
        transport=transport,
        is_windows=True,
        installer=_StatusInstaller(),
    ).status()

    assert status.installed is True
    assert status.code is expected_code
    assert status.detail == str(failure)


def test_broker_status_api_exposes_code_and_complete_detail() -> None:
    detail = "  line one\nline two: exact diagnostic\n"

    class Broker:
        def status(self):
            return {
                "installed": True,
                "healthy": False,
                "code": BrokerStatusFailureCode.STATUS_FAILED.value,
                "detail": detail,
            }

    app = types.SimpleNamespace(state=types.SimpleNamespace(web=types.SimpleNamespace(sandbox_broker=Broker())))
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/sandbox/status",
            "headers": [],
            "client": ("127.0.0.1", 1),
            "server": ("127.0.0.1", 8000),
            "scheme": "http",
            "app": app,
        }
    )

    assert status_broker(request) == {
        "installed": True,
        "healthy": False,
        "code": "broker_status_failed",
        "detail": detail,
    }


@pytest.mark.parametrize(("winerror", "expected"), [(1060, False), (None, True)])
def test_service_installed_only_returns_false_for_missing_service(
    monkeypatch: pytest.MonkeyPatch,
    winerror: int | None,
    expected: bool,
) -> None:
    class ServiceError(Exception):
        def __init__(self, code: int) -> None:
            super().__init__(code, "service error")
            self.winerror = code

    service_handle = object()
    fake_win32service = types.SimpleNamespace(
        SC_MANAGER_CONNECT=1,
        SERVICE_QUERY_STATUS=4,
        OpenSCManager=lambda *args: object(),
        OpenService=(
            (lambda *args: service_handle)
            if winerror is None
            else (lambda *args: (_ for _ in ()).throw(ServiceError(winerror)))
        ),
        CloseServiceHandle=lambda handle: None,
    )
    monkeypatch.setitem(sys.modules, "win32service", fake_win32service)
    installer = WindowsServiceInstaller(("pythonservice.exe",), is_windows=True)

    assert installer.service_installed() is expected


def test_service_installed_preserves_non_missing_query_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    class AccessDenied(Exception):
        winerror = 5

    fake_win32service = types.SimpleNamespace(
        SC_MANAGER_CONNECT=1,
        SERVICE_QUERY_STATUS=4,
        OpenSCManager=lambda *args: object(),
        OpenService=lambda *args: (_ for _ in ()).throw(AccessDenied("denied")),
        CloseServiceHandle=lambda handle: None,
    )
    monkeypatch.setitem(sys.modules, "win32service", fake_win32service)
    installer = WindowsServiceInstaller(("pythonservice.exe",), is_windows=True)

    with pytest.raises(AccessDenied, match="denied"):
        installer.service_installed()


def test_injected_runner_executes_one_local_transaction() -> None:
    calls: list[list[str]] = []

    def runner(command, **kwargs):
        calls.append(list(command))
        return _Result()

    installer = WindowsServiceInstaller(
        ("python.exe", "-m", "backend.sandbox.service_main", "run"),
        runner=runner,
        is_windows=True,
    )
    installer.install()

    assert [call[:2] for call in calls] == [["sc.exe", "create"], ["sc.exe", "sidtype"], ["sc.exe", "start"]]
    assert calls[0][calls[0].index("obj=") + 1] == r"NT SERVICE\MiniAgentSandboxBroker"


def test_pywin32_service_host_is_resolved_before_elevation(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    host = tmp_path / "runtime" / "pythonservice.exe"
    host.parent.mkdir()
    host.write_bytes(b"host")
    python_dll = tmp_path / "base" / "python312.dll"
    python_dll.parent.mkdir()
    python_dll.write_bytes(b"python-runtime")
    pywintypes_dll = tmp_path / "packages" / "pywintypes312.dll"
    pywintypes_dll.parent.mkdir()
    pywintypes_dll.write_bytes(b"pywin32-runtime")
    servicemanager_pyd = tmp_path / "packages" / "servicemanager.pyd"
    servicemanager_pyd.write_bytes(b"service-manager")
    module = types.SimpleNamespace(LocatePythonServiceExe=lambda: str(host))
    monkeypatch.setitem(sys.modules, "win32serviceutil", module)
    monkeypatch.setitem(
        sys.modules,
        "win32api",
        types.SimpleNamespace(GetModuleFileName=lambda _handle: str(python_dll)),
    )
    monkeypatch.setitem(sys.modules, "pywintypes", types.SimpleNamespace(__file__=str(pywintypes_dll)))
    monkeypatch.setitem(sys.modules, "servicemanager", types.SimpleNamespace(__file__=str(servicemanager_pyd)))
    installer = WindowsServiceInstaller(
        ("placeholder.exe",),
        service_class="sandbox_service_bootstrap.MiniAgentSandboxBrokerService",
        is_windows=True,
    )

    installer._prepare_service_host()

    assert installer.service_command == (str(host.resolve()),)
    assert (host.parent / python_dll.name).read_bytes() == b"python-runtime"
    assert (host.parent / pywintypes_dll.name).read_bytes() == b"pywin32-runtime"
    assert (host.parent / servicemanager_pyd.name).read_bytes() == b"service-manager"


def test_python_service_path_lists_only_explicit_import_roots(tmp_path: Path) -> None:
    executable = tmp_path / "venv" / "pythonservice.exe"
    executable.parent.mkdir()
    base = tmp_path / "base"
    environment = tmp_path / "venv"

    path_file = _write_python_service_path(executable, base, environment)

    assert path_file.read_text(encoding="utf-8").splitlines() == [
        ".",
        str((base / f"python{sys.version_info.major}{sys.version_info.minor}.zip").resolve()),
        str((base / "Lib").resolve()),
        str((base / "DLLs").resolve()),
        str((environment / "Lib" / "site-packages").resolve()),
        str((environment / "Lib" / "site-packages" / "win32").resolve()),
        str((environment / "Lib" / "site-packages" / "win32" / "lib").resolve()),
    ]


@pytest.mark.skipif(os.name != "nt", reason="Windows service ACL test")
def test_injected_runner_uses_prefixed_sid_for_acl(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    class Result:
        returncode = 0

    monkeypatch.setattr(Path, "write_text", lambda self, *args, **kwargs: None)
    monkeypatch.setattr(Path, "read_text", lambda self, *args, **kwargs: "S-1-5-21-1-2-3-500")

    def runner(command, **kwargs):
        calls.append(list(command))
        return Result()

    installer = WindowsServiceInstaller(
        ("python.exe", "-m", "backend.sandbox.service_main", "run"),
        runner=runner,
        is_windows=True,
        backend_sid_path=Path("C:/ProgramData/Mini-Agent/SandboxBroker/backend.sid"),
        program_data_path=Path("C:/ProgramData/Mini-Agent/SandboxBroker"),
        service_code_path=Path("C:/workspace/mini_agent/backend/src"),
        service_code_boundary_path=Path("C:/workspace/mini_agent"),
    )
    installer._run_local_transaction("repair", "S-1-5-21-1-2-3-500")

    program_data_call = next(call for call in calls if call[:2] == ["icacls.exe", str(installer.program_data_path)])
    sid_calls = [call for call in calls if call[:2] == ["icacls.exe", str(installer.backend_sid_path)]]
    key_path = installer.program_data_path / "installation.key.dpapi"
    takeown_call = next(call for call in calls if call[:3] == ["takeown.exe", "/F", str(key_path)])
    key_acl_call = next(call for call in calls if call[:2] == ["icacls.exe", str(key_path)])
    source_call = next(call for call in calls if call[:2] == ["win32-acl", str(installer.service_code_path)])
    service_sid = _service_sid("MiniAgentSandboxBroker")

    assert "*S-1-5-21-1-2-3-500:(OI)(CI)(M)" in program_data_call
    assert len(sid_calls) == 2
    assert f"*{service_sid}:(R)" not in sid_calls[0]
    assert f"*{service_sid}:(R)" in sid_calls[1]
    assert takeown_call[-1] == "/A"
    assert "*S-1-5-21-1-2-3-500:(R)" in key_acl_call
    assert f"*{service_sid}:(M)" in key_acl_call
    assert source_call[2:] == [service_sid, "RX", "inherit"]
    assert [call for call in calls if call[0] == "win32-acl"] == [
        ["win32-acl", str(installer.service_code_boundary_path), service_sid, "X", "direct"],
        [
            "win32-acl",
            str(installer.service_code_boundary_path / "backend"),
            service_sid,
            "X",
            "direct",
        ],
        source_call,
    ]


def test_injected_repair_installs_when_service_is_missing() -> None:
    calls: list[list[str]] = []

    def runner(command, **kwargs):
        calls.append(list(command))
        return _Result(1 if command[:2] == ["sc.exe", "query"] else 0)

    installer = WindowsServiceInstaller(("python.exe", "-m", "broker"), runner=runner, is_windows=True)
    installer.repair()

    assert calls[0][:3] == ["sc.exe", "query", "MiniAgentSandboxBroker"]
    assert calls[1][:2] == ["sc.exe", "create"]


def test_injected_repair_reconfigures_service_and_accepts_already_running() -> None:
    calls: list[list[str]] = []

    def runner(command, **kwargs):
        calls.append(list(command))
        if command[:2] == ["sc.exe", "start"]:
            return _Result(1056)
        if command[:2] == ["sc.exe", "stop"]:
            return _Result(1062)
        return _Result()

    installer = WindowsServiceInstaller(("python.exe", "-m", "broker"), runner=runner, is_windows=True)
    installer.repair()

    assert calls[1] == ["sc.exe", "stop", "MiniAgentSandboxBroker"]
    config = calls[2]
    assert config[:2] == ["sc.exe", "config"]
    assert config[config.index("obj=") + 1] == r"NT SERVICE\MiniAgentSandboxBroker"
    assert config[config.index("binPath=") + 1] == "python.exe -m broker"
    assert ["sc.exe", "sidtype", "MiniAgentSandboxBroker", "unrestricted"] in calls


def _install_fake_pywin32(monkeypatch: pytest.MonkeyPatch, shell_execute, *, exit_code: int = 0):
    shell_module = types.ModuleType("win32com.shell")
    shell_module.shell = types.SimpleNamespace(ShellExecuteEx=shell_execute)
    win32com = types.ModuleType("win32com")
    win32com.shell = shell_module
    monkeypatch.setitem(sys.modules, "win32com", win32com)
    monkeypatch.setitem(sys.modules, "win32com.shell", shell_module)
    monkeypatch.setitem(sys.modules, "win32con", types.SimpleNamespace(SEE_MASK_NOCLOSEPROCESS=64, SW_HIDE=0))
    monkeypatch.setitem(
        sys.modules,
        "win32event",
        types.SimpleNamespace(INFINITE=-1, WaitForSingleObject=lambda handle, timeout: 0),
    )
    monkeypatch.setitem(
        sys.modules,
        "win32process",
        types.SimpleNamespace(GetExitCodeProcess=lambda handle: exit_code),
    )
    monkeypatch.setitem(sys.modules, "win32api", types.SimpleNamespace(CloseHandle=lambda handle: None))


def test_elevated_transaction_uses_current_source_directory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[dict[str, object]] = []
    source_root = tmp_path / "backend" / "src"

    def shell_execute(**kwargs):
        calls.append(kwargs)
        return {"hProcess": object()}

    _install_fake_pywin32(monkeypatch, shell_execute)
    installer = WindowsServiceInstaller(
        ("python.exe", "-m", "broker"),
        is_windows=True,
        service_code_path=source_root,
        service_code_boundary_path=tmp_path,
    )
    installer._run_elevated_transaction("install", None)

    assert len(calls) == 1
    assert calls[0]["lpVerb"] == "runas"
    assert calls[0]["lpFile"] == sys.executable
    assert "backend.sandbox.install_helper" in str(calls[0]["lpParameters"])
    assert calls[0]["lpDirectory"] == str(source_root)


def test_elevated_helper_bootstrap_executes_the_declared_source_root(tmp_path: Path) -> None:
    source_root = tmp_path / "src"
    sandbox_package = source_root / "sandbox"
    sandbox_package.mkdir(parents=True)
    (source_root / "__init__.py").write_text("", encoding="utf-8")
    (sandbox_package / "__init__.py").write_text("", encoding="utf-8")
    (sandbox_package / "install_helper.py").write_text("def main():\n    return 73\n", encoding="utf-8")

    result = subprocess.run(
        [sys.executable, *_elevated_helper_argv("payload", source_root)],
        check=False,
        capture_output=True,
    )

    assert result.returncode == 73


def test_elevated_transaction_classifies_uac_cancel(monkeypatch: pytest.MonkeyPatch) -> None:
    class UacCancelledError(RuntimeError):
        winerror = 1223

    def shell_execute(**kwargs):
        raise UacCancelledError()

    _install_fake_pywin32(monkeypatch, shell_execute)
    installer = WindowsServiceInstaller(("python.exe", "-m", "broker"), is_windows=True)

    with pytest.raises(BrokerInstallationError) as raised:
        installer._run_elevated_transaction("install", None)

    assert raised.value.broker_code is BrokerInstallFailureCode.UAC_CANCELLED
    assert "UAC" in raised.value.safe_message


@pytest.mark.parametrize(
    ("exit_code", "expected_code"),
    [
        (4, BrokerInstallFailureCode.ACL_FAILED),
        (5, BrokerInstallFailureCode.SERVICE_START_FAILED),
        (7, BrokerInstallFailureCode.SERVICE_STOP_FAILED),
    ],
)
def test_elevated_transaction_classifies_helper_phases(
    monkeypatch: pytest.MonkeyPatch,
    exit_code: int,
    expected_code: BrokerInstallFailureCode,
) -> None:
    _install_fake_pywin32(monkeypatch, lambda **kwargs: {"hProcess": object()}, exit_code=exit_code)
    installer = WindowsServiceInstaller(("python.exe", "-m", "broker"), is_windows=True)

    with pytest.raises(BrokerInstallationError) as raised:
        installer._run_elevated_transaction("repair", None)

    assert raised.value.broker_code is expected_code


def test_install_route_returns_safe_category_and_code() -> None:
    class Broker:
        def status(self):
            return {"installed": False, "healthy": False}

        def install(self):
            raise BrokerInstallationError(
                BrokerInstallFailureCode.ADMIN_REQUIRED,
                "需要管理员权限才能安装沙箱 Broker。",
            )

    app = types.SimpleNamespace(
        state=types.SimpleNamespace(
            web=types.SimpleNamespace(
                sandbox_broker=Broker(),
                auth_service=types.SimpleNamespace(origin_allowed=lambda request: True),
            )
        )
    )
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/sandbox/install",
            "headers": [],
            "client": ("127.0.0.1", 1),
            "server": ("127.0.0.1", 8000),
            "scheme": "http",
            "app": app,
        }
    )

    response = install_broker(request)

    assert response.status_code == 503
    assert json.loads(response.body) == {
        "detail": "需要管理员权限才能安装沙箱 Broker。",
        "code": "broker_admin_required",
    }


def test_repair_route_returns_safe_stop_category_and_code() -> None:
    message = "Broker Windows 服务未能停止，请稍后重试或重启 Windows。"

    class Broker:
        def status(self):
            return {"installed": True, "healthy": False}

        def repair(self):
            raise BrokerInstallationError(BrokerInstallFailureCode.SERVICE_STOP_FAILED, message)

    app = types.SimpleNamespace(
        state=types.SimpleNamespace(
            web=types.SimpleNamespace(
                sandbox_broker=Broker(),
                auth_service=types.SimpleNamespace(origin_allowed=lambda request: True),
            )
        )
    )
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/sandbox/repair",
            "headers": [],
            "client": ("127.0.0.1", 1),
            "server": ("127.0.0.1", 8000),
            "scheme": "http",
            "app": app,
        }
    )

    response = repair_broker(request)

    assert response.status_code == 503
    assert json.loads(response.body) == {
        "detail": message,
        "code": "broker_service_stop_failed",
    }


@pytest.mark.parametrize("failure_code", list(BrokerInstallFailureCode))
def test_repair_route_preserves_every_failure_code_and_complete_detail(
    failure_code: BrokerInstallFailureCode,
) -> None:
    message = f"  raw repair detail for {failure_code.value}\nsecond line\n"

    class Broker:
        def status(self):
            return {"installed": True, "healthy": False}

        def repair(self):
            raise BrokerInstallationError(failure_code, message)

    app = types.SimpleNamespace(
        state=types.SimpleNamespace(
            web=types.SimpleNamespace(
                sandbox_broker=Broker(),
                auth_service=types.SimpleNamespace(origin_allowed=lambda request: True),
            )
        )
    )
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/sandbox/repair",
            "headers": [],
            "client": ("127.0.0.1", 1),
            "server": ("127.0.0.1", 8000),
            "scheme": "http",
            "app": app,
        }
    )

    response = repair_broker(request)

    assert response.status_code == 503
    assert json.loads(response.body) == {"detail": message, "code": failure_code.value}


def test_repair_route_installs_when_broker_is_missing() -> None:
    calls: list[str] = []

    class Broker:
        def status(self):
            return {"installed": False, "healthy": False}

        def install(self):
            calls.append("install")
            return {"installed": True, "healthy": True}

        def repair(self):
            calls.append("repair")
            return {"installed": True, "healthy": True}

    app = types.SimpleNamespace(
        state=types.SimpleNamespace(
            web=types.SimpleNamespace(
                sandbox_broker=Broker(),
                auth_service=types.SimpleNamespace(origin_allowed=lambda request: True),
            )
        )
    )
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/sandbox/repair",
            "headers": [],
            "client": ("127.0.0.1", 1),
            "server": ("127.0.0.1", 8000),
            "scheme": "http",
            "app": app,
        }
    )

    assert repair_broker(request) == {"installed": True, "healthy": True}
    assert calls == ["install"]


def test_maintenance_gate_rejects_overlap_in_both_directions() -> None:
    gate = SandboxMaintenanceGate()
    command = gate.acquire_command()
    with pytest.raises(SandboxMaintenanceBusy):
        gate.acquire_maintenance()
    command.close()

    maintenance = gate.acquire_maintenance()
    with pytest.raises(SandboxMaintenanceBusy):
        gate.acquire_command()
    maintenance.close()
    assert gate.active_commands == 0
    assert gate.maintenance_active is False


def test_reinstall_route_forces_healthy_broker_replacement(tmp_path: Path) -> None:
    calls: list[str] = []

    class Broker:
        def status(self):
            return {"installed": True, "healthy": True}

        def reclaim_stale(self):
            calls.append("reclaim")
            return ()

        def reinstall(self):
            calls.append("reinstall")
            return {"installed": True, "healthy": True}

    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    (runtime_dir / "sandbox-leases.json").write_text("{}", encoding="utf-8")
    web = types.SimpleNamespace(
        sandbox_broker=Broker(),
        sandbox_maintenance=SandboxMaintenanceGate(),
        sandbox_manifest_path=tmp_path / "resources.json",
        paths=types.SimpleNamespace(runtime_dir=runtime_dir),
    )
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/sandbox/reinstall",
            "headers": [],
            "client": ("127.0.0.1", 1),
            "server": ("127.0.0.1", 8000),
            "scheme": "http",
            "app": types.SimpleNamespace(state=types.SimpleNamespace(web=web)),
        }
    )

    assert reinstall_broker(request) == {"installed": True, "healthy": True}
    assert calls == ["reinstall"]
    assert not (runtime_dir / "sandbox-leases.json").exists()


def test_reinstall_route_replaces_web_state_client_after_key_rotation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    old_client = WindowsBrokerClient(is_windows=False)
    replacement_status = types.SimpleNamespace(
        healthy=True,
        to_dict=lambda: {"installed": True, "healthy": True},
    )
    replacement = types.SimpleNamespace(status=lambda: replacement_status)
    monkeypatch.setattr(old_client, "status", lambda: {"installed": True, "healthy": True})
    monkeypatch.setattr(old_client, "reinstall", lambda: {"installed": True, "healthy": True})
    monkeypatch.setattr(
        WindowsBrokerClient,
        "from_system",
        classmethod(lambda _cls, **_kwargs: replacement),
    )
    web = types.SimpleNamespace(
        sandbox_broker=old_client,
        sandbox_maintenance=SandboxMaintenanceGate(),
        sandbox_manifest_path=tmp_path / "resources.json",
        paths=types.SimpleNamespace(runtime_dir=tmp_path),
        settings=types.SimpleNamespace(sandbox_config=lambda: {"proxy_port": 17831}),
    )
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/sandbox/reinstall",
            "headers": [],
            "client": ("127.0.0.1", 1),
            "server": ("127.0.0.1", 8000),
            "scheme": "http",
            "app": types.SimpleNamespace(state=types.SimpleNamespace(web=web)),
        }
    )

    assert reinstall_broker(request) == {"installed": True, "healthy": True}
    assert web.sandbox_broker is replacement


def test_reinstall_route_rejects_active_command_without_touching_broker(tmp_path: Path) -> None:
    gate = SandboxMaintenanceGate()
    command = gate.acquire_command()
    web = types.SimpleNamespace(
        sandbox_broker=types.SimpleNamespace(),
        sandbox_maintenance=gate,
        sandbox_manifest_path=tmp_path / "resources.json",
        paths=types.SimpleNamespace(runtime_dir=tmp_path),
    )
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/sandbox/reinstall",
            "headers": [],
            "client": ("127.0.0.1", 1),
            "server": ("127.0.0.1", 8000),
            "scheme": "http",
            "app": types.SimpleNamespace(state=types.SimpleNamespace(web=web)),
        }
    )
    try:
        response = reinstall_broker(request)
    finally:
        command.close()

    assert response.status_code == 409
    assert json.loads(response.body)["code"] == "broker_jobs_active"


@pytest.mark.parametrize(
    ("manifest", "expected_status", "expected_code"),
    [
        ({"records": [{"job_id": "job-active"}]}, 409, "broker_jobs_active"),
        ({"unexpected": []}, 503, "broker_install_failed"),
    ],
)
def test_reinstall_route_rejects_unreclaimed_or_invalid_manifest(
    tmp_path: Path,
    manifest: dict[str, object],
    expected_status: int,
    expected_code: str,
) -> None:
    calls: list[str] = []

    class Broker:
        def status(self):
            return {"installed": True, "healthy": True}

        def reclaim_stale(self):
            calls.append("reclaim")
            return ()

        def reinstall(self):
            calls.append("reinstall")
            return {"installed": True, "healthy": True}

    manifest_path = tmp_path / "resources.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    web = types.SimpleNamespace(
        sandbox_broker=Broker(),
        sandbox_maintenance=SandboxMaintenanceGate(),
        sandbox_manifest_path=manifest_path,
        paths=types.SimpleNamespace(runtime_dir=tmp_path),
    )
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/sandbox/reinstall",
            "headers": [],
            "client": ("127.0.0.1", 1),
            "server": ("127.0.0.1", 8000),
            "scheme": "http",
            "app": types.SimpleNamespace(state=types.SimpleNamespace(web=web)),
        }
    )

    response = reinstall_broker(request)

    assert response.status_code == expected_status
    assert json.loads(response.body)["code"] == expected_code
    assert calls == (["reclaim"] if expected_status == 409 else [])


def test_reinstall_route_reclaims_confirmed_stale_manifest_before_replacement(tmp_path: Path) -> None:
    calls: list[str] = []
    manifest_path = tmp_path / "resources.json"
    manifest_path.write_text(json.dumps({"records": [{"job_id": "stale"}]}), encoding="utf-8")

    class Broker:
        def status(self):
            return {"installed": True, "healthy": True}

        def reclaim_stale(self):
            calls.append("reclaim")
            manifest_path.write_text(json.dumps({"records": []}), encoding="utf-8")
            return ("stale",)

        def reinstall(self):
            calls.append("reinstall")
            return {"installed": True, "healthy": True}

    web = types.SimpleNamespace(
        sandbox_broker=Broker(),
        sandbox_maintenance=SandboxMaintenanceGate(),
        sandbox_manifest_path=manifest_path,
        paths=types.SimpleNamespace(runtime_dir=tmp_path),
    )
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/sandbox/reinstall",
            "headers": [],
            "client": ("127.0.0.1", 1),
            "server": ("127.0.0.1", 8000),
            "scheme": "http",
            "app": types.SimpleNamespace(state=types.SimpleNamespace(web=web)),
        }
    )

    assert reinstall_broker(request) == {"installed": True, "healthy": True}
    assert calls == ["reclaim", "reinstall"]


def test_local_reinstall_transaction_deletes_then_recreates_service() -> None:
    calls: list[list[str]] = []
    installer = WindowsServiceInstaller(
        ("C:/runtime/pythonservice.exe",),
        service_class="sandbox_service_bootstrap.MiniAgentSandboxBrokerService",
        runner=lambda command, **_kwargs: calls.append(list(command)) or _Result(),
        is_windows=True,
    )

    installer._run_local_transaction("reinstall", None)

    assert calls[0] == ["sc.exe", "stop", "MiniAgentSandboxBroker"]
    assert calls[1] == ["sc.exe", "delete", "MiniAgentSandboxBroker"]
    assert calls[2][:3] == ["sc.exe", "create", "MiniAgentSandboxBroker"]
    assert calls[-1] == ["sc.exe", "start", "MiniAgentSandboxBroker"]


def test_reinstall_removes_ready_marker_before_destructive_steps(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    data_path = tmp_path / "Mini-Agent" / "SandboxBroker"
    data_path.mkdir(parents=True)
    ready_path = data_path / "ready.json"
    ready_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        "backend.sandbox.install_helper._uninstall_for_reinstall",
        lambda *_args: (_ for _ in ()).throw(_TransactionFailure(EXIT_SERVICE_STOP_FAILED, "stop failed")),
    )

    with pytest.raises(_TransactionFailure):
        run_transaction(
            {
                "operation": "reinstall",
                "service_name": "MiniAgentSandboxBroker",
                "service_command": ["python.exe"],
                "program_data_path": str(data_path),
            }
        )

    assert not ready_path.exists()


def test_account_cleanup_preserves_codex_identities_when_package_is_legacy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class MissingIdentityError(RuntimeError):
        def __init__(self, winerror: int) -> None:
            super().__init__(winerror)
            self.winerror = winerror

    deleted: list[str] = []
    fake_net = types.SimpleNamespace(
        NetUserGetInfo=lambda *_args: (_ for _ in ()).throw(MissingIdentityError(2221)),
        NetLocalGroupGetInfo=lambda *_args: (_ for _ in ()).throw(MissingIdentityError(2220)),
        NetUserDel=lambda _server, name: deleted.append(name),
        NetLocalGroupDel=lambda _server, name: deleted.append(name),
    )
    monkeypatch.setitem(sys.modules, "win32net", fake_net)
    monkeypatch.setitem(sys.modules, "win32security", types.SimpleNamespace())
    monkeypatch.setattr(
        "backend.sandbox.broker_service.credentials.DpapiCredentialStore.load",
        lambda _self: BrokerCredentialPackage(
            "legacy",
            "CodexSandboxOffline",
            "S-1-5-21-1-2-3-1001",
            "offline",
            "CodexSandboxOnline",
            "S-1-5-21-1-2-3-1002",
            "online",
        ),
    )

    _remove_owned_accounts(tmp_path)

    assert deleted == []


def test_account_cleanup_fails_closed_when_credentials_are_missing_for_managed_identity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    fake_net = types.SimpleNamespace(NetUserGetInfo=lambda *_args: {"name": "MiniSbxOffline"})
    monkeypatch.setitem(sys.modules, "win32net", fake_net)
    monkeypatch.setitem(sys.modules, "win32security", types.SimpleNamespace())
    monkeypatch.setattr(
        "backend.sandbox.broker_service.credentials.DpapiCredentialStore.load",
        lambda _self: (_ for _ in ()).throw(SandboxInitializationError("missing")),
    )

    with pytest.raises(_TransactionFailure) as raised:
        _remove_owned_accounts(tmp_path)

    assert raised.value.exit_code == EXIT_ACCOUNT_FAILED


@pytest.mark.parametrize("missing_account_rights", [False, True])
def test_account_cleanup_deletes_only_fully_verified_mini_agent_identities(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    missing_account_rights: bool,
) -> None:
    offline_sid = "S-1-5-21-1-2-3-2001"
    online_sid = "S-1-5-21-1-2-3-2002"
    deleted: list[str] = []

    class FakeNet:
        @staticmethod
        def NetLocalGroupGetInfo(_server, name, _level):
            assert name == "MiniAgentSandboxUsers"
            return {"comment": "Mini-Agent sandbox users (managed)"}

        @staticmethod
        def NetLocalGroupGetMembers(_server, name, _level):
            assert name == "MiniAgentSandboxUsers"
            return (
                [
                    {"domainandname": r"HOST\MiniSbxOffline"},
                    {"domainandname": r"HOST\MiniSbxOnline"},
                ],
                2,
                0,
            )

        @staticmethod
        def NetUserGetInfo(_server, name, _level):
            assert name in {"MiniSbxOffline", "MiniSbxOnline"}
            return {"priv": 0, "comment": "Mini-Agent sandbox account (managed)"}

        @staticmethod
        def NetUserGetLocalGroups(_server, _name, _level):
            return ["MiniAgentSandboxUsers"]

        @staticmethod
        def NetUserDel(_server, name):
            deleted.append(name)

        @staticmethod
        def NetLocalGroupDel(_server, name):
            deleted.append(name)

    sid_by_name = {
        "MiniAgentSandboxUsers": "group-sid",
        "MiniSbxOffline": offline_sid,
        "MiniSbxOnline": online_sid,
    }

    class MissingAccountRightsError(RuntimeError):
        winerror = 2

    def remove_account_rights(*_args):
        if missing_account_rights:
            raise MissingAccountRightsError("no account rights")

    fake_security = types.SimpleNamespace(
        POLICY_ALL_ACCESS=1,
        LsaOpenPolicy=lambda *_args: "policy",
        LookupAccountName=lambda _server, name: (sid_by_name[name], "HOST", 1),
        ConvertSidToStringSid=lambda sid: sid,
        LsaRemoveAccountRights=remove_account_rights,
    )
    monkeypatch.setitem(sys.modules, "win32net", FakeNet())
    monkeypatch.setitem(sys.modules, "win32security", fake_security)
    monkeypatch.setattr(
        "backend.sandbox.broker_service.credentials.DpapiCredentialStore.load",
        lambda _self: BrokerCredentialPackage(
            "current",
            "MiniSbxOffline",
            offline_sid,
            "offline",
            "MiniSbxOnline",
            online_sid,
            "online",
        ),
    )

    _remove_owned_accounts(tmp_path)

    assert deleted == ["MiniSbxOffline", "MiniSbxOnline", "MiniAgentSandboxUsers"]


@pytest.mark.parametrize("stop_returncode", [0, 1])
def test_repair_waits_for_stopped_after_any_stop_result(stop_returncode: int) -> None:
    calls: list[list[str]] = []
    states = iter([3, 1])

    _stop_service_for_repair(
        "MiniAgentSandboxBroker",
        runner=lambda command, **kwargs: calls.append(list(command)) or _Result(stop_returncode),
        state_reader=lambda service_name: next(states),
        clock=lambda: 0.0,
        sleeper=lambda seconds: None,
    )

    assert calls == [["sc.exe", "stop", "MiniAgentSandboxBroker"]]


def test_repair_stop_timeout_has_dedicated_exit_code() -> None:
    ticks = iter([0.0, 0.0, 5.0])

    with pytest.raises(_TransactionFailure) as raised:
        _stop_service_for_repair(
            "MiniAgentSandboxBroker",
            runner=lambda command, **kwargs: _Result(),
            state_reader=lambda service_name: 4,
            clock=lambda: next(ticks),
            sleeper=lambda seconds: None,
        )

    assert raised.value.exit_code == EXIT_SERVICE_STOP_FAILED


def test_repair_state_query_failure_aborts_before_config(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(
        "backend.sandbox.install_helper.subprocess.run",
        lambda command, **kwargs: calls.append(list(command)) or _Result(),
    )
    monkeypatch.setattr(
        "backend.sandbox.install_helper._query_service_state",
        lambda service_name: (_ for _ in ()).throw(OSError("unavailable")),
    )

    with pytest.raises(_TransactionFailure) as raised:
        run_transaction(
            {
                "operation": "repair",
                "service_name": "MiniAgentSandboxBroker",
                "service_command": ["python.exe", "-m", "backend.sandbox.service_main", "run"],
                "service_class": "sandbox_service_bootstrap.MiniAgentSandboxBrokerService",
                "backend_sid": None,
                "backend_sid_path": None,
                "program_data_path": None,
                "service_code_path": None,
                "service_code_boundary_path": None,
            }
        )

    assert raised.value.exit_code == EXIT_SERVICE_STOP_FAILED
    assert calls == [["sc.exe", "stop", "MiniAgentSandboxBroker"]]


@pytest.mark.skipif(os.name != "nt", reason="Windows icacls test")
def test_numeric_sid_is_prefixed_for_icacls(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _icacls_sid("S-1-5-21-1-2-3-500") == "*S-1-5-21-1-2-3-500"
    with pytest.raises(ValueError):
        _icacls_sid("Administrator")

    calls: list[list[str]] = []

    class Result:
        returncode = 0

    monkeypatch.setattr(Path, "read_text", lambda self, **kwargs: "S-1-5-21-1-2-3-500")
    monkeypatch.setattr(
        "backend.sandbox.install_helper.subprocess.run",
        lambda command, **kwargs: calls.append(list(command)) or Result(),
    )
    _secure_program_data(
        Path("C:/ProgramData/Mini-Agent/SandboxBroker"),
        Path("C:/ProgramData/Mini-Agent/SandboxBroker/backend.sid"),
        "MiniAgentSandboxBroker",
    )

    assert "*S-1-5-21-1-2-3-500:(OI)(CI)(M)" in calls[0]
    assert f"*{_service_sid('MiniAgentSandboxBroker')}:(R)" in calls[1]


def test_service_sid_matches_windows_virtual_account() -> None:
    assert _service_sid("MiniAgentSandboxBroker") == ("S-1-5-80-2596524395-1801458667-1993906640-1419760394-1149293312")


def test_persist_sid_atomically_replaces_an_existing_file(tmp_path: Path) -> None:
    sid_path = tmp_path / "backend.sid"
    sid_path.write_text("stale", encoding="ascii")

    _persist_sid(sid_path, "S-1-5-21-1-2-3-500")

    assert sid_path.read_text(encoding="ascii") == "S-1-5-21-1-2-3-500"
    assert list(tmp_path.glob(".backend.sid.*")) == []


def test_invalid_sid_is_rejected_before_replacing_existing_file(tmp_path: Path) -> None:
    sid_path = tmp_path / "backend.sid"
    sid_path.write_text("existing", encoding="ascii")

    with pytest.raises(ValueError):
        _persist_sid(sid_path, "Administrator")

    assert sid_path.read_text(encoding="ascii") == "existing"


@pytest.mark.skipif(os.name != "nt", reason="Windows source ACL test")
def test_source_acl_is_confined_to_rx_on_source_tree() -> None:
    source = Path("C:/workspace/mini_agent/backend/src")
    boundary = Path("C:/workspace/mini_agent")

    grants = _source_acl_grants(source, boundary, "MiniAgentSandboxBroker")
    service_sid = _service_sid("MiniAgentSandboxBroker")

    assert [grant.path for grant in grants] == [boundary, boundary / "backend", source]
    assert [grant.rights for grant in grants] == ["X", "X", "RX"]
    assert [grant.inherit for grant in grants] == [False, False, True]
    assert grants[-1].sid == service_sid
    assert grants[-1].rights == "RX"
    assert grants[-1].inherit is True


def test_runtime_acl_requires_the_service_executable_and_grants_rx() -> None:
    runtime = Path("C:/workspace/mini_agent/.venv")
    base_runtime = Path("C:/python/cpython-3.12")
    executable = runtime / "Scripts" / "python.exe"

    grants = _runtime_acl_grants((runtime, base_runtime, runtime), executable, "MiniAgentSandboxBroker")

    assert [grant.path for grant in grants] == [runtime, base_runtime]
    assert all(grant.rights == "RX" and grant.inherit for grant in grants)
    with pytest.raises(ValueError):
        _runtime_acl_grants((base_runtime,), executable, "MiniAgentSandboxBroker")


def test_helper_transaction_orders_sid_service_acl_source_and_start(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[list[str]] = []
    program_data = tmp_path / "Mini-Agent" / "SandboxBroker"
    sid_path = program_data / "backend.sid"
    source = tmp_path / "repo" / "backend" / "src"

    monkeypatch.setattr(
        "backend.sandbox.install_helper.subprocess.run",
        lambda command, **kwargs: calls.append(list(command)) or _Result(),
    )
    monkeypatch.setattr(
        "backend.sandbox.install_helper._secure_source_code",
        lambda path, boundary, service_name: calls.append(["win32-acl-batch", str(path), str(boundary), service_name]),
    )
    monkeypatch.setattr(
        "backend.sandbox.install_helper._wait_for_service_stopped",
        lambda service_name, **kwargs: True,
    )
    package = BrokerCredentialPackage(
        "generation-test",
        "MiniSbxOffline",
        "S-1-5-21-1-2-3-1001",
        "offline-password",
        "MiniSbxOnline",
        "S-1-5-21-1-2-3-1002",
        "online-password",
    )
    monkeypatch.setattr(
        "backend.sandbox.install_helper._provision_fixed_accounts",
        lambda *_args: (package, build_ready_marker(package, 17831)),
    )

    assert (
        run_transaction(
            {
                "operation": "repair",
                "service_name": "MiniAgentSandboxBroker",
                "service_command": ["python.exe", "-m", "backend.sandbox.service_main", "run"],
                "service_class": rf"{source}\sandbox_service_bootstrap.MiniAgentSandboxBrokerService",
                "backend_sid": "S-1-5-21-1-2-3-500",
                "backend_sid_path": str(sid_path),
                "program_data_path": str(program_data),
                "service_code_path": str(source),
                "service_code_boundary_path": str(tmp_path / "repo"),
            }
        )
        == 0
    )

    assert calls[0][:2] == ["icacls.exe", str(sid_path)]
    service_sid = f"*{_service_sid('MiniAgentSandboxBroker')}"
    assert f"{service_sid}:(R)" not in calls[0]
    assert calls[1] == ["sc.exe", "stop", "MiniAgentSandboxBroker"]
    assert calls[2][:2] == ["sc.exe", "config"]
    assert calls[3] == [
        "reg.exe",
        "add",
        r"HKLM\SYSTEM\CurrentControlSet\Services\MiniAgentSandboxBroker\PythonClass",
        "/ve",
        "/t",
        "REG_SZ",
        "/d",
        rf"{source}\sandbox_service_bootstrap.MiniAgentSandboxBrokerService",
        "/f",
    ]
    assert calls[4] == ["sc.exe", "sidtype", "MiniAgentSandboxBroker", "unrestricted"]
    assert calls[5][:2] == ["icacls.exe", str(program_data)]
    assert calls[6][:2] == ["icacls.exe", str(sid_path)]
    assert f"{service_sid}:(R)" in calls[6]
    assert calls[-4] == ["win32-acl-batch", str(source), str(tmp_path / "repo"), "MiniAgentSandboxBroker"]
    assert calls[-3] == ["takeown.exe", "/F", str(program_data / "ready.json"), "/A"]
    assert calls[-2][:2] == ["icacls.exe", str(program_data / "ready.json")]
    assert "*S-1-5-21-1-2-3-500:(R)" in calls[-2]
    assert calls[-1] == ["sc.exe", "start", "MiniAgentSandboxBroker"]
