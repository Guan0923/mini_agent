"""Windows service install/repair transaction and process adapter contract."""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Protocol

from ..errors import BrokerInstallationError, BrokerInstallFailureCode, SandboxInitializationError
from ..runtime.manifest import ResourceRecord


class BrokerProcessAdapter(Protocol):
    def reserve(self, request: Mapping[str, Any]) -> Mapping[str, Any]: ...

    def launch(self, request: Mapping[str, Any]) -> Mapping[str, Any]: ...

    def control(self, operation: str, request: Mapping[str, Any]) -> Mapping[str, Any]: ...

    def release(self, request: Mapping[str, Any]) -> bool: ...

    def reclaim(self, request: Mapping[str, Any]) -> tuple[str, ...]: ...

    def recover(self, record: ResourceRecord) -> bool: ...


class WindowsServiceInstaller:
    """Install/repair the Broker as a virtual-account Windows service."""

    def __init__(
        self,
        service_command: tuple[str, ...],
        *,
        service_name: str = "MiniAgentSandboxBroker",
        service_class: str | None = None,
        runner: Callable[..., Any] | None = None,
        is_windows: bool | None = None,
        backend_sid_path: Path | None = None,
        program_data_path: Path | None = None,
        service_code_path: Path | None = None,
        service_code_boundary_path: Path | None = None,
        service_runtime_paths: tuple[Path, ...] = (),
        proxy_port: int = 17831,
    ) -> None:
        if not service_command or any(not isinstance(item, str) or not item for item in service_command):
            raise ValueError("service_command must contain non-empty strings")
        self.service_command = service_command
        self.service_name = service_name
        self.service_class = service_class
        self._runner_injected = runner is not None
        self.runner = runner or subprocess.run
        self.is_windows = os.name == "nt" if is_windows is None else is_windows
        self.backend_sid_path = Path(backend_sid_path) if backend_sid_path is not None else None
        self.program_data_path = Path(program_data_path) if program_data_path is not None else None
        self.service_code_path = Path(service_code_path) if service_code_path is not None else None
        self.service_code_boundary_path = (
            Path(service_code_boundary_path) if service_code_boundary_path is not None else None
        )
        self.service_runtime_paths = tuple(Path(path) for path in service_runtime_paths)
        if (self.service_code_path is None) != (self.service_code_boundary_path is None):
            raise ValueError("service_code_path and service_code_boundary_path must be provided together")
        if any(not path.is_absolute() or len(path.parts) < 3 for path in self.service_runtime_paths):
            raise ValueError("service_runtime_paths must contain safe absolute paths")
        if isinstance(proxy_port, bool) or not isinstance(proxy_port, int) or not 1 <= proxy_port <= 65535:
            raise ValueError("proxy_port must be between 1 and 65535")
        self.proxy_port = proxy_port

    def install(self) -> None:
        self._require_windows()
        self._run_transaction("install")

    def repair(self) -> None:
        self._require_windows()
        query = self.runner(["sc.exe", "query", self.service_name], check=False, capture_output=True)
        if getattr(query, "returncode", 1) != 0:
            self._run_transaction("install")
            return

        self._run_transaction("repair")

    def configuration_healthy(self) -> bool:
        """Verify the unprivileged, non-secret SCM configuration summary."""

        if not self.is_windows:
            return False
        manager = None
        service = None
        win32service = None
        try:
            import winreg

            import win32service as win32service_module  # type: ignore[import-not-found]

            win32service = win32service_module
            manager = win32service.OpenSCManager(None, None, win32service.SC_MANAGER_CONNECT)
            service = win32service.OpenService(manager, self.service_name, win32service.SERVICE_QUERY_CONFIG)
            config = win32service.QueryServiceConfig(service)
            sid_type = win32service.QueryServiceConfig2(service, win32service.SERVICE_CONFIG_SERVICE_SID_INFO)
            expected_command = subprocess.list2cmdline(list(self.service_command))
            if (
                int(config[0]) != win32service.SERVICE_WIN32_OWN_PROCESS
                or int(config[1]) != win32service.SERVICE_DEMAND_START
                or str(config[3]).casefold() != expected_command.casefold()
                or str(config[7]).casefold() != f"NT SERVICE\\{self.service_name}".casefold()
                or int(sid_type) != win32service.SERVICE_SID_TYPE_UNRESTRICTED
            ):
                return False
            if self.service_class is None:
                return True
            key_path = rf"SYSTEM\CurrentControlSet\Services\{self.service_name}\PythonClass"
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path, 0, winreg.KEY_READ) as key:
                value, value_type = winreg.QueryValueEx(key, "")
            return value_type == winreg.REG_SZ and value == self.service_class
        except Exception:
            return False
        finally:
            if win32service is not None and service is not None:
                try:
                    win32service.CloseServiceHandle(service)
                except Exception:
                    pass
            if win32service is not None and manager is not None:
                try:
                    win32service.CloseServiceHandle(manager)
                except Exception:
                    pass

    def service_installed(self) -> bool:
        """Return whether SCM contains this service, preserving other query failures."""

        if not self.is_windows:
            return False
        manager = None
        service = None
        win32service = None
        try:
            import win32service as win32service_module  # type: ignore[import-not-found]

            win32service = win32service_module
            manager = win32service.OpenSCManager(None, None, win32service.SC_MANAGER_CONNECT)
            service = win32service.OpenService(manager, self.service_name, win32service.SERVICE_QUERY_STATUS)
            return True
        except Exception as exc:
            winerror = getattr(exc, "winerror", None)
            if winerror is None and getattr(exc, "args", None):
                first = exc.args[0]
                winerror = first if isinstance(first, int) else None
            if winerror == 1060:
                return False
            raise
        finally:
            if win32service is not None and service is not None:
                try:
                    win32service.CloseServiceHandle(service)
                except Exception:
                    pass
            if win32service is not None and manager is not None:
                try:
                    win32service.CloseServiceHandle(manager)
                except Exception:
                    pass

    def _run_transaction(self, operation: str) -> None:
        backend_sid = self._current_user_sid()
        if self._runner_injected:
            self._run_local_transaction(operation, backend_sid)
            return
        self._prepare_service_host()
        self._run_elevated_transaction(operation, backend_sid)

    def _prepare_service_host(self) -> None:
        if self.service_class is None:
            return
        try:
            import pywintypes  # type: ignore[import-not-found]
            import servicemanager  # type: ignore[import-not-found]
            import win32api  # type: ignore[import-not-found]
            import win32serviceutil  # type: ignore[import-not-found]

            executable_path = Path(win32serviceutil.LocatePythonServiceExe()).resolve()
            python_dll = Path(win32api.GetModuleFileName(sys.dllhandle)).resolve()
            runtime_binaries = [
                python_dll,
                Path(pywintypes.__file__).resolve(),
                Path(servicemanager.__file__).resolve(),
            ]
            runtime_binaries.extend(
                candidate
                for name in ("python3.dll", "vcruntime140.dll", "vcruntime140_1.dll")
                if (candidate := python_dll.parent / name).is_file()
            )
            for source in runtime_binaries:
                target = executable_path.parent / source.name
                if not target.exists() or target.stat().st_size != source.stat().st_size:
                    shutil.copy2(source, target)
            _write_python_service_path(executable_path, Path(sys.base_prefix), Path(sys.prefix))
            executable_path.with_suffix("._pth").unlink(missing_ok=True)
            executable = str(executable_path)
        except Exception as exc:  # pragma: no cover - Windows install path
            raise BrokerInstallationError(
                BrokerInstallFailureCode.DEPENDENCY_MISSING,
                "缺少 Windows Broker 服务宿主，请重新安装后端依赖。",
            ) from exc
        self.service_command = (executable,)

    def _run_local_transaction(self, operation: str, backend_sid: str | None) -> None:
        """Execute the same transaction through an injected test runner."""

        sid: str | None = None
        if self.backend_sid_path is not None and backend_sid is not None:
            try:
                self.backend_sid_path.parent.mkdir(parents=True, exist_ok=True)
                self.backend_sid_path.write_text(backend_sid, encoding="ascii")
                sid = backend_sid
            except OSError as exc:
                raise BrokerInstallationError(
                    BrokerInstallFailureCode.ACL_FAILED,
                    "Broker 文件权限配置失败，请以管理员权限重试。",
                ) from exc
        from ..install_helper import (
            _managed_file_acl_commands,
            _program_data_acl_commands,
            _runtime_acl_grants,
            _sensitive_file_acl_commands,
            _service_class_command,
            _sid_acl_command,
            _source_acl_grants,
        )

        command = subprocess.list2cmdline(list(self.service_command))
        commands: list[list[str]] = []
        if self.backend_sid_path is not None and sid is not None:
            commands.append(_sid_acl_command(self.backend_sid_path, sid, None))
        if operation == "install":
            commands.extend(
                [
                    [
                        "sc.exe",
                        "create",
                        self.service_name,
                        "type=",
                        "own",
                        "start=",
                        "demand",
                        "obj=",
                        f"NT SERVICE\\{self.service_name}",
                        "binPath=",
                        command,
                    ],
                    *([_service_class_command(self.service_name, self.service_class)] if self.service_class else []),
                    ["sc.exe", "sidtype", self.service_name, "unrestricted"],
                ]
            )
        else:
            commands.extend(
                [
                    ["sc.exe", "stop", self.service_name],
                    [
                        "sc.exe",
                        "config",
                        self.service_name,
                        "type=",
                        "own",
                        "start=",
                        "demand",
                        "obj=",
                        f"NT SERVICE\\{self.service_name}",
                        "binPath=",
                        command,
                    ],
                    *([_service_class_command(self.service_name, self.service_class)] if self.service_class else []),
                    ["sc.exe", "sidtype", self.service_name, "unrestricted"],
                ]
            )
        if self.program_data_path is not None and self.backend_sid_path is not None:
            try:
                # Build the ACL command through the same validation as the
                # elevated helper, while executing it with the injected runner.
                persisted_sid = self.backend_sid_path.read_text(encoding="ascii").strip()
                commands.extend(
                    _program_data_acl_commands(
                        self.program_data_path,
                        self.backend_sid_path,
                        persisted_sid,
                        self.service_name,
                    )
                )
                for name in (
                    "installation.id",
                    "installation.key.dpapi",
                    "ready.json",
                    "control-plane.jsonl",
                    "resources.json",
                ):
                    commands.extend(
                        _managed_file_acl_commands(
                            self.program_data_path / name,
                            persisted_sid,
                            self.service_name,
                        )
                    )
                commands.extend(
                    _sensitive_file_acl_commands(
                        self.program_data_path / "accounts.dpapi",
                        self.service_name,
                    )
                )
            except (OSError, ValueError) as exc:
                raise BrokerInstallationError(
                    BrokerInstallFailureCode.ACL_FAILED,
                    "Broker 文件权限配置失败，请以管理员权限重试。",
                ) from exc
        if self.service_code_path is not None and self.service_code_boundary_path is not None:
            try:
                commands.extend(
                    grant.runner_command()
                    for grant in _source_acl_grants(
                        self.service_code_path, self.service_code_boundary_path, self.service_name
                    )
                )
            except ValueError as exc:
                raise BrokerInstallationError(
                    BrokerInstallFailureCode.ACL_FAILED,
                    "Broker 文件权限配置失败，请以管理员权限重试。",
                ) from exc
        try:
            commands.extend(
                grant.runner_command()
                for grant in _runtime_acl_grants(
                    self.service_runtime_paths,
                    Path(self.service_command[0]),
                    self.service_name,
                )
            )
        except ValueError as exc:
            raise BrokerInstallationError(
                BrokerInstallFailureCode.ACL_FAILED,
                "Broker 运行时权限配置失败，请以管理员权限重试。",
            ) from exc
        commands.append(["sc.exe", "start", self.service_name])
        for command_args in commands:
            result = self.runner(command_args, check=False, capture_output=True)
            returncode = int(getattr(result, "returncode", 1))
            service_already_running = command_args[:2] == ["sc.exe", "start"] and returncode == 1056
            service_already_stopped = command_args[:2] == ["sc.exe", "stop"] and returncode == 1062
            if returncode != 0 and not service_already_running and not service_already_stopped:
                if command_args[0].lower() in {"icacls.exe", "takeown.exe", "win32-acl"}:
                    failure_code = BrokerInstallFailureCode.ACL_FAILED
                    message = "Broker 文件权限配置失败，请以管理员权限重试。"
                elif len(command_args) > 1 and command_args[1].lower() == "start":
                    failure_code = BrokerInstallFailureCode.SERVICE_START_FAILED
                    message = "Broker Windows 服务启动失败。"
                elif len(command_args) > 1 and command_args[1].lower() == "stop":
                    failure_code = BrokerInstallFailureCode.SERVICE_STOP_FAILED
                    message = "Broker Windows 服务未能停止，请稍后重试或重启 Windows。"
                else:
                    failure_code = BrokerInstallFailureCode.SERVICE_FAILED
                    message = "Windows 服务创建或配置失败。"
                raise BrokerInstallationError(
                    failure_code,
                    message,
                )

    def _run_elevated_transaction(self, operation: str, backend_sid: str | None) -> None:
        """Run the complete control-plane operation behind one UAC prompt."""

        try:
            import win32con  # type: ignore[import-not-found]
            import win32event  # type: ignore[import-not-found]
            import win32process  # type: ignore[import-not-found]
            from win32com.shell import shell  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - Windows install path
            raise BrokerInstallationError(
                BrokerInstallFailureCode.DEPENDENCY_MISSING,
                "缺少 Windows Broker 安装依赖，请重新安装后端依赖。",
            ) from exc

        payload = {
            "operation": operation,
            "service_name": self.service_name,
            "service_command": list(self.service_command),
            "service_class": self.service_class,
            "backend_sid": backend_sid,
            "backend_sid_path": str(self.backend_sid_path) if self.backend_sid_path is not None else None,
            "program_data_path": str(self.program_data_path) if self.program_data_path is not None else None,
            "service_code_path": str(self.service_code_path) if self.service_code_path is not None else None,
            "service_code_boundary_path": (
                str(self.service_code_boundary_path) if self.service_code_boundary_path is not None else None
            ),
            "service_runtime_paths": [str(path) for path in self.service_runtime_paths],
            "proxy_port": self.proxy_port,
        }
        encoded = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8")).decode("ascii")
        parameters = subprocess.list2cmdline(["-m", "backend.sandbox.install_helper", encoded])
        handle: Any | None = None
        try:  # pragma: no cover - requires an interactive Windows desktop
            result = shell.ShellExecuteEx(
                fMask=getattr(win32con, "SEE_MASK_NOCLOSEPROCESS", 0x00000040),
                lpVerb="runas",
                lpFile=sys.executable,
                lpParameters=parameters,
                nShow=getattr(win32con, "SW_HIDE", 0),
            )
            handle = result["hProcess"] if isinstance(result, Mapping) else result
            win32event.WaitForSingleObject(handle, win32event.INFINITE)
            code = int(win32process.GetExitCodeProcess(handle))
        except Exception as exc:
            winerror = getattr(exc, "winerror", None)
            if winerror is None and getattr(exc, "args", None):
                first = exc.args[0]
                winerror = first if isinstance(first, int) else None
            if winerror == 1223:
                raise BrokerInstallationError(
                    BrokerInstallFailureCode.UAC_CANCELLED,
                    "安装已取消，请在 UAC 提示中批准 Broker 安装。",
                ) from exc
            if winerror in {5, 740}:
                raise BrokerInstallationError(
                    BrokerInstallFailureCode.ADMIN_REQUIRED,
                    "需要管理员权限才能安装沙箱 Broker。",
                ) from exc
            raise BrokerInstallationError(
                BrokerInstallFailureCode.UNKNOWN,
                "沙箱 Broker 安装失败，请查看后端日志。",
            ) from exc
        finally:
            if handle is not None:
                try:
                    import win32api  # type: ignore[import-not-found]

                    win32api.CloseHandle(handle)
                except Exception:
                    pass
        if code == 0:
            return
        from ..install_helper import (
            EXIT_ACCOUNT_FAILED,
            EXIT_ACL_FAILED,
            EXIT_CREDENTIAL_FAILED,
            EXIT_FILESYSTEM_FAILED,
            EXIT_INVALID,
            EXIT_NETWORK_FAILED,
            EXIT_RIGHTS_FAILED,
            EXIT_SERVICE_START_FAILED,
            EXIT_SERVICE_STOP_FAILED,
        )

        if code == EXIT_FILESYSTEM_FAILED:
            raise BrokerInstallationError(
                BrokerInstallFailureCode.ACL_FAILED,
                "Broker 文件权限配置失败，请以管理员权限重试。",
            )
        if code == EXIT_ACL_FAILED:
            raise BrokerInstallationError(
                BrokerInstallFailureCode.ACL_FAILED,
                "Broker 文件权限配置失败，请以管理员权限重试。",
            )
        if code == EXIT_ACCOUNT_FAILED:
            raise BrokerInstallationError(
                BrokerInstallFailureCode.ACCOUNT_FAILED,
                "沙箱固定账户创建或安全纳管失败；请检查是否存在同名高权限账户。",
            )
        if code == EXIT_CREDENTIAL_FAILED:
            raise BrokerInstallationError(
                BrokerInstallFailureCode.CREDENTIAL_FAILED,
                "沙箱账户凭据创建、验证或加密保存失败。",
            )
        if code == EXIT_RIGHTS_FAILED:
            raise BrokerInstallationError(
                BrokerInstallFailureCode.PRIVILEGE_FAILED,
                "沙箱登录权限或 Broker 服务权限配置失败。",
            )
        if code == EXIT_NETWORK_FAILED:
            raise BrokerInstallationError(
                BrokerInstallFailureCode.NETWORK_FAILED,
                "沙箱静态网络隔离策略配置失败。",
            )
        if code == EXIT_SERVICE_START_FAILED:
            raise BrokerInstallationError(
                BrokerInstallFailureCode.SERVICE_START_FAILED,
                "Broker Windows 服务启动失败。",
            )
        if code == EXIT_SERVICE_STOP_FAILED:
            raise BrokerInstallationError(
                BrokerInstallFailureCode.SERVICE_STOP_FAILED,
                "Broker Windows 服务未能停止，请稍后重试或重启 Windows。",
            )
        if code == EXIT_INVALID:
            raise BrokerInstallationError(
                BrokerInstallFailureCode.UNKNOWN,
                "沙箱 Broker 安装失败，请查看后端日志。",
            )
        raise BrokerInstallationError(
            BrokerInstallFailureCode.SERVICE_FAILED,
            "Windows 服务创建或启动失败。",
        )

    def _current_user_sid(self) -> str | None:
        if self.backend_sid_path is None:
            return None
        try:
            import win32api  # type: ignore[import-not-found]
            import win32con  # type: ignore[import-not-found]
            import win32security  # type: ignore[import-not-found]

            token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
            sid = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
            return str(win32security.ConvertSidToStringSid(sid))
        except ImportError as exc:  # pragma: no cover - Windows install path
            raise BrokerInstallationError(
                BrokerInstallFailureCode.DEPENDENCY_MISSING,
                "缺少 Windows Broker 安装依赖，请重新安装后端依赖。",
            ) from exc
        except Exception as exc:  # pragma: no cover - Windows install path
            raise BrokerInstallationError(
                BrokerInstallFailureCode.UNKNOWN,
                "无法读取当前 Windows 用户身份，请查看后端日志。",
            ) from exc

    def _require_windows(self) -> None:
        if not self.is_windows:
            raise SandboxInitializationError("Windows Broker service is unavailable on this platform")


def _write_python_service_path(executable: Path, base_prefix: Path, environment_prefix: Path) -> Path:
    path_file = executable.parent / f"python{sys.version_info.major}{sys.version_info.minor}._pth"
    site_packages = environment_prefix / "Lib" / "site-packages"
    entries = (
        ".",
        str((base_prefix / f"python{sys.version_info.major}{sys.version_info.minor}.zip").resolve()),
        str((base_prefix / "Lib").resolve()),
        str((base_prefix / "DLLs").resolve()),
        str(site_packages.resolve()),
        str((site_packages / "win32").resolve()),
        str((site_packages / "win32" / "lib").resolve()),
    )
    if any("\n" in entry or "\r" in entry for entry in entries):
        raise ValueError("Broker Python runtime path is invalid")
    content = "\n".join(entries) + "\n"
    try:
        if path_file.read_text(encoding="utf-8") == content:
            return path_file
    except OSError:
        pass
    temporary = path_file.with_name(f".{path_file.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(content, encoding="utf-8", newline="\n")
        os.replace(temporary, path_file)
    finally:
        temporary.unlink(missing_ok=True)
    return path_file
