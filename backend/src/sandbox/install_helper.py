"""Privileged, single-transaction Windows Broker installer.

This module is launched once through UAC by :class:`WindowsServiceInstaller`.
It intentionally contains no network or application state and returns only a
small exit code to the unprivileged parent process.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

EXIT_OK = 0
EXIT_INVALID = 2
EXIT_SERVICE_FAILED = 3
EXIT_ACL_FAILED = 4
EXIT_SERVICE_START_FAILED = 5
EXIT_FILESYSTEM_FAILED = 6
EXIT_SERVICE_STOP_FAILED = 7
EXIT_ACCOUNT_FAILED = 8
EXIT_CREDENTIAL_FAILED = 9
EXIT_RIGHTS_FAILED = 10
EXIT_NETWORK_FAILED = 11

_SERVICE_STOPPED = 1
_SERVICE_STOP_TIMEOUT_SECONDS = 5.0
_SERVICE_STOP_POLL_SECONDS = 0.1
_BROKER_SERVICE_CLASS = "sandbox_service_bootstrap.MiniAgentSandboxBrokerService"
_OFFLINE_ACCOUNT = "MiniSbxOffline"
_ONLINE_ACCOUNT = "MiniSbxOnline"
_ACCOUNT_GROUP = "MiniAgentSandboxUsers"
_ACCOUNT_COMMENT = "Mini-Agent sandbox account (managed)"
_GROUP_COMMENT = "Mini-Agent sandbox users (managed)"


class _TransactionFailure(RuntimeError):
    def __init__(self, exit_code: int, message: str) -> None:
        super().__init__(message)
        self.exit_code = exit_code


def _powershell_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _icacls_sid(value: str) -> str:
    """Format a numeric SID for icacls (numeric SIDs require a leading *)."""

    sid = value.strip()
    if not re.fullmatch(r"S-\d+(?:-\d+)+", sid, flags=re.IGNORECASE):
        raise ValueError("Broker backend SID is invalid")
    return f"*{sid}"


def _service_sid(service_name: str) -> str:
    """Return the deterministic Windows virtual-service SID for a service name."""

    digest = hashlib.sha1(service_name.upper().encode("utf-16le")).digest()
    authorities = struct.unpack("<5I", digest)
    return "S-1-5-80-" + "-".join(str(authority) for authority in authorities)


def _service_class_command(service_name: str, service_class: str) -> list[str]:
    return [
        "reg.exe",
        "add",
        rf"HKLM\SYSTEM\CurrentControlSet\Services\{service_name}\PythonClass",
        "/ve",
        "/t",
        "REG_SZ",
        "/d",
        service_class,
        "/f",
    ]


def _validate_payload(
    payload: Mapping[str, Any],
) -> tuple[
    str,
    str,
    tuple[str, ...],
    str | None,
    str | None,
    Path | None,
    Path | None,
    Path | None,
    Path | None,
    tuple[Path, ...],
    int,
]:
    operation = payload.get("operation")
    service_name = payload.get("service_name")
    service_command = payload.get("service_command")
    service_class = payload.get("service_class")
    backend_sid = payload.get("backend_sid")
    backend_sid_path = payload.get("backend_sid_path")
    program_data_path = payload.get("program_data_path")
    service_code_path = payload.get("service_code_path")
    service_code_boundary_path = payload.get("service_code_boundary_path")
    service_runtime_paths = payload.get("service_runtime_paths", [])
    proxy_port = payload.get("proxy_port", 17831)
    if operation not in {"install", "repair", "reinstall"} or not isinstance(service_name, str) or not service_name:
        raise ValueError("invalid Broker installation operation")
    if (
        not isinstance(service_command, list)
        or not service_command
        or any(not isinstance(item, str) or not item for item in service_command)
    ):
        raise ValueError("invalid Broker service command")
    if service_class is not None and (
        not isinstance(service_class, str) or not service_class or "\r" in service_class or "\n" in service_class
    ):
        raise ValueError("invalid Broker service class")
    if backend_sid is not None and (not isinstance(backend_sid, str) or not backend_sid):
        raise ValueError("invalid Broker backend SID")
    if backend_sid_path is not None and not isinstance(backend_sid_path, str):
        raise ValueError("invalid Broker SID path")
    if program_data_path is not None and not isinstance(program_data_path, str):
        raise ValueError("invalid Broker ProgramData path")
    if service_code_path is not None and not isinstance(service_code_path, str):
        raise ValueError("invalid Broker source path")
    if service_code_boundary_path is not None and not isinstance(service_code_boundary_path, str):
        raise ValueError("invalid Broker source boundary path")
    if not isinstance(service_runtime_paths, list) or any(
        not isinstance(item, str) or not item for item in service_runtime_paths
    ):
        raise ValueError("invalid Broker runtime paths")
    sid_path = Path(backend_sid_path) if backend_sid_path else None
    data_path = Path(program_data_path) if program_data_path else None
    code_path = Path(service_code_path) if service_code_path else None
    code_boundary_path = Path(service_code_boundary_path) if service_code_boundary_path else None
    runtime_paths = tuple(Path(item) for item in service_runtime_paths)
    if sid_path is not None and not sid_path.is_absolute():
        raise ValueError("Broker SID path must be absolute")
    if data_path is not None and not data_path.is_absolute():
        raise ValueError("Broker ProgramData path must be absolute")
    if data_path is not None and (
        data_path.name.casefold() != "sandboxbroker" or data_path.parent.name.casefold() != "mini-agent"
    ):
        raise ValueError("Broker ProgramData path is outside the managed directory")
    if code_path is not None and not code_path.is_absolute():
        raise ValueError("Broker source path must be absolute")
    if code_boundary_path is not None and not code_boundary_path.is_absolute():
        raise ValueError("Broker source boundary path must be absolute")
    if (code_path is None) != (code_boundary_path is None):
        raise ValueError("Broker source path and boundary must be provided together")
    expected_service_class = _BROKER_SERVICE_CLASS if code_path is None else rf"{code_path}\{_BROKER_SERVICE_CLASS}"
    if service_class is not None and os.path.normcase(service_class) != os.path.normcase(expected_service_class):
        raise ValueError("Broker service class is outside the declared source path")
    if any(not path.is_absolute() or len(path.parts) < 3 for path in runtime_paths):
        raise ValueError("Broker runtime path must be a safe absolute path")
    if isinstance(proxy_port, bool) or not isinstance(proxy_port, int) or not 1 <= proxy_port <= 65535:
        raise ValueError("Broker proxy port is invalid")
    return (
        operation,
        service_name,
        tuple(service_command),
        service_class,
        backend_sid,
        sid_path,
        data_path,
        code_path,
        code_boundary_path,
        runtime_paths,
        proxy_port,
    )


def _run(
    command: Sequence[str],
    *,
    failure_code: int = EXIT_SERVICE_FAILED,
    accepted_returncodes: frozenset[int] = frozenset({0}),
) -> None:
    try:
        result = subprocess.run(command, check=False, capture_output=True)
    except OSError as exc:
        raise _TransactionFailure(EXIT_FILESYSTEM_FAILED, "Broker service command could not start") from exc
    if result.returncode not in accepted_returncodes:
        raise _TransactionFailure(failure_code, "Broker service command failed")


def _query_service_state(service_name: str) -> int:
    """Read the numeric SCM state without parsing localized command output."""

    manager = None
    service = None
    win32service = None
    try:
        import win32service as win32service_module  # type: ignore[import-not-found]

        win32service = win32service_module
        manager = win32service.OpenSCManager(None, None, win32service.SC_MANAGER_CONNECT)
        service = win32service.OpenService(manager, service_name, win32service.SERVICE_QUERY_STATUS)
        status = win32service.QueryServiceStatusEx(service)
        return int(status["CurrentState"])
    except Exception as exc:
        raise OSError("Broker service state is unavailable") from exc
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


def _wait_for_service_stopped(
    service_name: str,
    *,
    state_reader: Callable[[str], int] | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], Any] = time.sleep,
) -> bool:
    reader = state_reader or _query_service_state
    deadline = clock() + _SERVICE_STOP_TIMEOUT_SECONDS
    while True:
        try:
            state = int(reader(service_name))
        except Exception:
            return False
        if state == _SERVICE_STOPPED:
            return True
        remaining = deadline - clock()
        if remaining <= 0:
            return False
        sleeper(min(_SERVICE_STOP_POLL_SECONDS, remaining))


def _stop_service_for_repair(
    service_name: str,
    *,
    runner: Callable[..., Any] | None = None,
    state_reader: Callable[[str], int] | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], Any] = time.sleep,
) -> None:
    command_runner = runner or subprocess.run
    try:
        command_runner(["sc.exe", "stop", service_name], check=False, capture_output=True)
    except OSError:
        # SCM state is authoritative: a command failure may race with the
        # service reaching STOPPED, so always perform the bounded state check.
        pass
    if _wait_for_service_stopped(
        service_name,
        state_reader=state_reader,
        clock=clock,
        sleeper=sleeper,
    ):
        return
    raise _TransactionFailure(EXIT_SERVICE_STOP_FAILED, "Broker service did not stop")


def _service_exists(service_name: str) -> bool:
    try:
        result = subprocess.run(["sc.exe", "query", service_name], check=False, capture_output=True)
    except OSError as exc:
        raise OSError("Broker service state is unavailable") from exc
    return int(result.returncode) == 0


def _wait_for_service_deleted(
    service_name: str,
    *,
    exists: Callable[[str], bool] | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], Any] = time.sleep,
) -> bool:
    checker = exists or _service_exists
    deadline = clock() + _SERVICE_STOP_TIMEOUT_SECONDS
    while True:
        try:
            present = bool(checker(service_name))
        except Exception:
            return False
        if not present:
            return True
        remaining = deadline - clock()
        if remaining <= 0:
            return False
        sleeper(min(_SERVICE_STOP_POLL_SECONDS, remaining))


def _persist_sid(path: Path | None, value: str | None) -> None:
    if path is None or value is None:
        return
    _icacls_sid(value)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Replace the file instead of writing in place.  Older installations
        # may have an empty/protected DACL on backend.sid, which can deny an
        # administrator an in-place write.  The parent directory is already
        # controlled by the elevated transaction and permits replacement.
        fd, temporary = tempfile.mkstemp(prefix=".backend.sid.", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="ascii") as stream:
                stream.write(value)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
    except OSError as exc:
        raise OSError("Broker backend SID could not be persisted") from exc


def _sid_acl_command(
    path: Path,
    backend_sid: str,
    service_name: str | None,
) -> list[str]:
    grants = [
        "SYSTEM:(F)",
        "Administrators:(F)",
        f"{_icacls_sid(backend_sid)}:(M)",
    ]
    if service_name is not None:
        grants.append(f"{_icacls_sid(_service_sid(service_name))}:(R)")
    return [
        "icacls.exe",
        str(path),
        "/inheritance:r",
        "/grant:r",
        *grants,
        "/C",
    ]


def _program_data_acl_commands(
    path: Path,
    sid_path: Path,
    backend_sid: str,
    service_name: str,
) -> list[list[str]]:
    if not backend_sid or not path.is_absolute() or len(path.parts) < 3:
        raise ValueError("Broker ProgramData path is invalid")
    return [
        [
            "icacls.exe",
            str(path),
            "/inheritance:r",
            "/grant:r",
            "SYSTEM:(OI)(CI)(F)",
            "Administrators:(OI)(CI)(F)",
            f"{_icacls_sid(backend_sid)}:(OI)(CI)(M)",
            f"{_icacls_sid(_service_sid(service_name))}:(OI)(CI)(M)",
            "/T",
            "/C",
        ],
        _sid_acl_command(sid_path, backend_sid, service_name),
    ]


def _managed_file_acl_commands(path: Path, backend_sid: str, service_name: str) -> list[list[str]]:
    return [
        ["takeown.exe", "/F", str(path), "/A"],
        [
            "icacls.exe",
            str(path),
            "/inheritance:r",
            "/grant:r",
            "SYSTEM:(F)",
            "Administrators:(F)",
            f"{_icacls_sid(backend_sid)}:(R)",
            f"{_icacls_sid(_service_sid(service_name))}:(M)",
            "/C",
        ],
    ]


def _sensitive_file_acl_commands(path: Path, service_name: str) -> list[list[str]]:
    return [
        ["takeown.exe", "/F", str(path), "/A"],
        [
            "icacls.exe",
            str(path),
            "/inheritance:r",
            "/grant:r",
            "SYSTEM:(F)",
            "Administrators:(F)",
            f"{_icacls_sid(_service_sid(service_name))}:(R)",
            "/C",
        ],
    ]


def _directory_contains(path: Path, name: str) -> bool:
    try:
        with os.scandir(path) as entries:
            return any(entry.name.casefold() == name.casefold() for entry in entries)
    except OSError as exc:
        raise OSError("Broker ProgramData directory is unavailable") from exc


def _secure_program_data(path: Path | None, sid_path: Path | None, service_name: str) -> None:
    if path is None or sid_path is None:
        return
    try:
        backend_sid = sid_path.read_text(encoding="ascii").strip()
    except OSError as exc:
        raise OSError("Broker backend SID is unavailable") from exc
    for command in _program_data_acl_commands(path, sid_path, backend_sid, service_name):
        _run(command, failure_code=EXIT_ACL_FAILED)
    for name in (
        "installation.id",
        "installation.key.dpapi",
        "ready.json",
        "control-plane.jsonl",
        "resources.json",
    ):
        managed_path = path / name
        if _directory_contains(path, name):
            for command in _managed_file_acl_commands(managed_path, backend_sid, service_name):
                _run(command, failure_code=EXIT_ACL_FAILED)
    credential_path = path / "accounts.dpapi"
    if _directory_contains(path, credential_path.name):
        for command in _sensitive_file_acl_commands(credential_path, service_name):
            _run(command, failure_code=EXIT_ACL_FAILED)


def _provision_fixed_accounts(data_path: Path, service_name: str, proxy_port: int):
    try:
        import win32con  # type: ignore[import-not-found]
        import win32net  # type: ignore[import-not-found]
        import win32netcon  # type: ignore[import-not-found]
        import win32security  # type: ignore[import-not-found]

        from .broker_service.credentials import BrokerCredentialPackage, DpapiCredentialStore
        from .broker_service.readiness import build_ready_marker
    except ImportError as exc:
        raise OSError("Broker account dependencies are unavailable") from exc

    group_name = _ACCOUNT_GROUP
    try:
        win32net.NetLocalGroupAdd(None, 1, {"name": group_name, "comment": _GROUP_COMMENT})
    except Exception as exc:
        if getattr(exc, "winerror", None) not in {1379, 2223}:
            raise _TransactionFailure(EXIT_ACCOUNT_FAILED, "Broker sandbox group could not be created") from exc
        try:
            group_info = win32net.NetLocalGroupGetInfo(None, group_name, 1)
        except Exception as read_exc:
            raise _TransactionFailure(EXIT_ACCOUNT_FAILED, "Broker sandbox group is unavailable") from read_exc
        if str(group_info.get("comment") or "") != _GROUP_COMMENT:
            raise _TransactionFailure(EXIT_ACCOUNT_FAILED, "Conflicting sandbox group ownership")

    credential_store = DpapiCredentialStore(data_path / "accounts.dpapi")
    try:
        existing_package = credential_store.load()
    except Exception:
        existing_package = None

    accounts: dict[str, tuple[str, str]] = {}
    for role, name in (("offline", _OFFLINE_ACCOUNT), ("online", _ONLINE_ACCOUNT)):
        password = (
            getattr(existing_package, f"{role}_password", "")
            if existing_package is not None and getattr(existing_package, f"{role}_name", "") == name
            else ""
        )
        try:
            info = win32net.NetUserGetInfo(None, name, 4)
            _validate_existing_sandbox_user(name, info, win32net)
        except Exception as exc:
            if getattr(exc, "winerror", None) != 2221:
                if isinstance(exc, _TransactionFailure):
                    raise
                raise _TransactionFailure(EXIT_ACCOUNT_FAILED, "Conflicting sandbox account exists") from exc
            password = secrets_token()
            try:
                win32net.NetUserAdd(
                    None,
                    1,
                    {
                        "name": name,
                        "password": password,
                        # NetUserAdd level 1 rejects USER_PRIV_GUEST.  Without
                        # membership in the built-in Users group, Windows
                        # reports the persisted account as USER_PRIV_GUEST.
                        "priv": win32netcon.USER_PRIV_USER,
                        "flags": (
                            win32netcon.UF_SCRIPT
                            | win32netcon.UF_DONT_EXPIRE_PASSWD
                            | win32netcon.UF_PASSWD_CANT_CHANGE
                        ),
                        "comment": _ACCOUNT_COMMENT,
                    },
                )
            except Exception as create_exc:
                raise _TransactionFailure(
                    EXIT_ACCOUNT_FAILED, "Broker sandbox account could not be created"
                ) from create_exc
        try:
            sid, _, _ = win32security.LookupAccountName(None, name)
        except Exception as exc:
            raise _TransactionFailure(EXIT_ACCOUNT_FAILED, "Broker sandbox account SID is unavailable") from exc
        sid_text = str(win32security.ConvertSidToStringSid(sid))
        package_sid = getattr(existing_package, f"{role}_sid", "") if existing_package is not None else ""
        if not password or package_sid != sid_text or not _credential_works(name, password, win32security, win32con):
            password = secrets_token()
            try:
                win32net.NetUserSetInfo(None, name, 1003, {"password": password})
            except Exception as exc:
                raise _TransactionFailure(
                    EXIT_CREDENTIAL_FAILED, "Broker sandbox credential could not be rotated"
                ) from exc
        try:
            info = win32net.NetUserGetInfo(None, name, 4)
            flags = int(info.get("flags", 0)) | win32netcon.UF_DONT_EXPIRE_PASSWD | win32netcon.UF_PASSWD_CANT_CHANGE
            win32net.NetUserSetInfo(None, name, 1008, {"flags": flags})
        except Exception as exc:
            raise _TransactionFailure(EXIT_ACCOUNT_FAILED, "Broker sandbox account flags could not be set") from exc
        try:
            win32net.NetLocalGroupAddMembers(None, group_name, 3, [{"domainandname": name}])
        except Exception as exc:
            if getattr(exc, "winerror", None) != 1378:
                raise _TransactionFailure(EXIT_ACCOUNT_FAILED, "Broker sandbox group membership failed") from exc
        accounts[role] = (sid_text, password)

    group_rights = ("SeBatchLogonRight", "SeDenyInteractiveLogonRight", "SeDenyRemoteInteractiveLogonRight")
    service_rights = ("SeAssignPrimaryTokenPrivilege", "SeIncreaseQuotaPrivilege")
    try:
        group_sid, _, _ = win32security.LookupAccountName(None, group_name)
        policy_handle = win32security.LsaOpenPolicy(None, win32security.POLICY_ALL_ACCESS)
        win32security.LsaAddAccountRights(policy_handle, group_sid, group_rights)
        service_sid = win32security.ConvertStringSidToSid(_service_sid(service_name))
        win32security.LsaAddAccountRights(policy_handle, service_sid, service_rights)
        if not set(group_rights).issubset(set(win32security.LsaEnumerateAccountRights(policy_handle, group_sid))):
            raise OSError("Broker sandbox logon rights could not be verified")
        if not set(service_rights).issubset(set(win32security.LsaEnumerateAccountRights(policy_handle, service_sid))):
            raise OSError("Broker service privileges could not be verified")
    except Exception as exc:
        raise _TransactionFailure(EXIT_RIGHTS_FAILED, "Broker logon rights could not be configured") from exc
    generation = (
        existing_package.generation
        if existing_package is not None
        and existing_package.offline_sid == accounts["offline"][0]
        and existing_package.online_sid == accounts["online"][0]
        and existing_package.offline_password == accounts["offline"][1]
        and existing_package.online_password == accounts["online"][1]
        else f"generation-{uuid.uuid4().hex}"
    )
    package = BrokerCredentialPackage(
        generation,
        _OFFLINE_ACCOUNT,
        accounts["offline"][0],
        accounts["offline"][1],
        _ONLINE_ACCOUNT,
        accounts["online"][0],
        accounts["online"][1],
    )
    try:
        credential_store.save(package)
    except Exception as exc:
        raise _TransactionFailure(EXIT_CREDENTIAL_FAILED, "Broker credentials could not be persisted") from exc
    try:
        _configure_static_network(accounts["offline"][0], accounts["online"][0], proxy_port)
    except Exception as exc:
        raise _TransactionFailure(EXIT_NETWORK_FAILED, "Broker network policy could not be configured") from exc
    return package, build_ready_marker(package, proxy_port)


def _validate_existing_sandbox_user(name: str, info: Mapping[str, Any], win32net: Any) -> None:
    if int(info.get("priv", -1)) != 0:
        raise _TransactionFailure(EXIT_ACCOUNT_FAILED, "Conflicting sandbox account privilege")
    forbidden = {"administrators", "backup operators", "power users", "remote desktop users"}
    groups = {str(value).casefold() for value in win32net.NetUserGetLocalGroups(None, name, 0)}
    if groups & forbidden:
        raise _TransactionFailure(EXIT_ACCOUNT_FAILED, "Conflicting sandbox account group membership")
    if str(info.get("comment") or "") != _ACCOUNT_COMMENT:
        raise _TransactionFailure(EXIT_ACCOUNT_FAILED, "Conflicting sandbox account ownership")


def _credential_works(name: str, password: str, security: Any, win32con: Any) -> bool:
    try:
        token = security.LogonUser(name, ".", password, win32con.LOGON32_LOGON_BATCH, win32con.LOGON32_PROVIDER_DEFAULT)
        token.Close()
        return True
    except Exception:
        return False


def _remove_owned_accounts(data_path: Path) -> None:
    """Delete only accounts proven to belong to this Mini-Agent install."""

    try:
        import win32net  # type: ignore[import-not-found]
        import win32security  # type: ignore[import-not-found]

        from .broker_service.credentials import DpapiCredentialStore
    except ImportError as exc:
        raise OSError("Broker account dependencies are unavailable") from exc
    try:
        package = DpapiCredentialStore(data_path / "accounts.dpapi").load()
    except Exception:
        if _managed_identity_exists(win32net):
            raise _TransactionFailure(EXIT_ACCOUNT_FAILED, "Broker sandbox account ownership is unverified")
        return
    expected = {
        _OFFLINE_ACCOUNT: package.offline_sid if package.offline_name == _OFFLINE_ACCOUNT else None,
        _ONLINE_ACCOUNT: package.online_sid if package.online_name == _ONLINE_ACCOUNT else None,
    }
    if any(value is None for value in expected.values()):
        if _managed_identity_exists(win32net):
            raise _TransactionFailure(EXIT_ACCOUNT_FAILED, "Broker sandbox account ownership is unverified")
        return
    try:
        group_info = win32net.NetLocalGroupGetInfo(None, _ACCOUNT_GROUP, 1)
        raw_members = win32net.NetLocalGroupGetMembers(None, _ACCOUNT_GROUP, 3)[0]
        members = {
            str(value.get("domainandname") or "").casefold() for value in raw_members if isinstance(value, Mapping)
        }
    except Exception as exc:
        if getattr(exc, "winerror", None) in {1376, 2220}:
            if _managed_identity_exists(win32net):
                raise _TransactionFailure(EXIT_ACCOUNT_FAILED, "Broker sandbox account ownership is unverified")
            return
        raise _TransactionFailure(EXIT_ACCOUNT_FAILED, "Broker sandbox group could not be verified") from exc
    if str(group_info.get("comment") or "") != _GROUP_COMMENT:
        raise _TransactionFailure(EXIT_ACCOUNT_FAILED, "Conflicting sandbox group ownership")
    expected_members = {f"{os.environ.get('COMPUTERNAME', '.')}\\{name}".casefold() for name in expected}
    if members != expected_members and {member.rsplit("\\", 1)[-1] for member in members} != {
        name.casefold() for name in expected
    }:
        raise _TransactionFailure(EXIT_ACCOUNT_FAILED, "Conflicting sandbox group membership")
    policy_handle = None
    try:
        policy_handle = win32security.LsaOpenPolicy(None, win32security.POLICY_ALL_ACCESS)
        group_sid, _, _ = win32security.LookupAccountName(None, _ACCOUNT_GROUP)
        try:
            win32security.LsaRemoveAccountRights(policy_handle, group_sid, True, ())
        except Exception as exc:
            if getattr(exc, "winerror", None) not in {2, 1332}:
                raise
        for name, expected_sid in expected.items():
            info = win32net.NetUserGetInfo(None, name, 4)
            _validate_existing_sandbox_user(name, info, win32net)
            sid, _, _ = win32security.LookupAccountName(None, name)
            sid_text = str(win32security.ConvertSidToStringSid(sid))
            if str(info.get("comment") or "") != _ACCOUNT_COMMENT or sid_text != expected_sid:
                raise _TransactionFailure(EXIT_ACCOUNT_FAILED, "Conflicting sandbox account ownership")
        for name in expected:
            win32net.NetUserDel(None, name)
        win32net.NetLocalGroupDel(None, _ACCOUNT_GROUP)
    except _TransactionFailure:
        raise
    except Exception as exc:
        raise _TransactionFailure(EXIT_ACCOUNT_FAILED, "Broker sandbox accounts could not be removed") from exc


def _managed_identity_exists(win32net: Any) -> bool:
    for name in (_OFFLINE_ACCOUNT, _ONLINE_ACCOUNT):
        try:
            win32net.NetUserGetInfo(None, name, 0)
            return True
        except Exception as exc:
            if getattr(exc, "winerror", None) != 2221:
                raise _TransactionFailure(EXIT_ACCOUNT_FAILED, "Broker sandbox account could not be verified") from exc
    try:
        win32net.NetLocalGroupGetInfo(None, _ACCOUNT_GROUP, 0)
        return True
    except Exception as exc:
        if getattr(exc, "winerror", None) != 2220:
            raise _TransactionFailure(EXIT_ACCOUNT_FAILED, "Broker sandbox group could not be verified") from exc
    return False


def _remove_service_rights(service_name: str) -> None:
    try:
        import win32security  # type: ignore[import-not-found]

        policy_handle = win32security.LsaOpenPolicy(None, win32security.POLICY_ALL_ACCESS)
        service_sid = win32security.ConvertStringSidToSid(_service_sid(service_name))
        win32security.LsaRemoveAccountRights(policy_handle, service_sid, True, ())
    except Exception as exc:
        winerror = getattr(exc, "winerror", None)
        if winerror not in {2, 1332}:
            raise _TransactionFailure(EXIT_RIGHTS_FAILED, "Broker service rights could not be removed") from exc


def _remove_static_network() -> None:
    try:
        from .native_windows.wfp import remove_static_wfp

        remove_static_wfp()
    except Exception as exc:
        raise _TransactionFailure(EXIT_NETWORK_FAILED, "Broker network policy could not be removed") from exc


def secrets_token() -> str:
    import secrets

    return secrets.token_urlsafe(32)


def _configure_static_network(offline_sid: str, online_sid: str, proxy_port: int) -> None:
    from .native_windows.wfp import configure_static_wfp

    configure_static_wfp(offline_sid, online_sid, proxy_port)
    # Remove pre-v3 Defender Firewall rules only after the atomic WFP policy is
    # active.  This avoids both an enforcement gap and stale proxy-port rules.
    script = """
$ErrorActionPreference='Stop'
Get-NetFirewallRule -Name 'MiniAgentSandbox-*' -ErrorAction SilentlyContinue | Remove-NetFirewallRule
"""
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    _run(
        ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
        failure_code=EXIT_NETWORK_FAILED,
    )


@dataclass(frozen=True, slots=True)
class _SourceAclGrant:
    path: Path
    sid: str
    rights: str
    inherit: bool

    def runner_command(self) -> list[str]:
        return ["win32-acl", str(self.path), self.sid, self.rights, "inherit" if self.inherit else "direct"]


def _source_acl_grants(path: Path, boundary: Path, service_name: str) -> list[_SourceAclGrant]:
    if not path.is_absolute() or not boundary.is_absolute() or len(path.parts) < 3:
        raise ValueError("Broker source path is invalid")
    try:
        path.relative_to(boundary)
    except ValueError as exc:
        raise ValueError("Broker source path is outside its boundary") from exc
    if path == boundary:
        raise ValueError("Broker source path must be below its boundary")
    service_sid = _service_sid(service_name)
    relative = path.relative_to(boundary)
    ancestors = [boundary]
    current = boundary
    for component in relative.parts[:-1]:
        current /= component
        ancestors.append(current)
    return [
        *(_SourceAclGrant(ancestor, service_sid, "X", False) for ancestor in ancestors),
        _SourceAclGrant(path, service_sid, "RX", True),
    ]


def _runtime_acl_grants(paths: Sequence[Path], service_executable: Path, service_name: str) -> list[_SourceAclGrant]:
    resolved_paths = tuple(Path(path).resolve() for path in paths)
    executable = Path(service_executable).resolve()
    if resolved_paths and not any(path == executable or path in executable.parents for path in resolved_paths):
        raise ValueError("Broker executable is outside the declared runtime paths")
    service_sid = _service_sid(service_name)
    unique_paths = dict.fromkeys(resolved_paths)
    return [_SourceAclGrant(path, service_sid, "RX", True) for path in unique_paths]


def _apply_source_acl_grant(grant: _SourceAclGrant) -> None:
    try:
        import ntsecuritycon  # type: ignore[import-not-found]
        import win32security  # type: ignore[import-not-found]

        sid = win32security.ConvertStringSidToSid(grant.sid)
        descriptor = win32security.GetNamedSecurityInfo(
            str(grant.path),
            win32security.SE_FILE_OBJECT,
            win32security.DACL_SECURITY_INFORMATION,
        )
        dacl = descriptor.GetSecurityDescriptorDacl() or win32security.ACL()
        inheritance = win32security.CONTAINER_INHERIT_ACE | win32security.OBJECT_INHERIT_ACE if grant.inherit else 0
        access = (
            ntsecuritycon.FILE_GENERIC_READ | ntsecuritycon.FILE_GENERIC_EXECUTE
            if grant.rights == "RX"
            else ntsecuritycon.FILE_TRAVERSE
        )
        for index in range(dacl.GetAceCount()):
            ace = dacl.GetAce(index)
            if (
                ace[0][0] == win32security.ACCESS_ALLOWED_ACE_TYPE
                and ace[2] == sid
                and ace[1] & access == access
                and ace[0][1] & inheritance == inheritance
            ):
                return
        dacl.AddAccessAllowedAceEx(win32security.ACL_REVISION_DS, inheritance, access, sid)
        win32security.SetNamedSecurityInfo(
            str(grant.path),
            win32security.SE_FILE_OBJECT,
            win32security.DACL_SECURITY_INFORMATION,
            None,
            None,
            dacl,
            None,
        )
    except Exception as exc:
        raise OSError("Broker source ACL could not be configured") from exc


def _secure_source_code(path: Path | None, boundary: Path | None, service_name: str) -> None:
    if path is None or boundary is None:
        return
    for grant in _source_acl_grants(path, boundary, service_name):
        _apply_source_acl_grant(grant)


def _remove_source_acl_grant(path: Path, sid_text: str) -> None:
    if not path.exists():
        return
    try:
        import win32security  # type: ignore[import-not-found]

        sid = win32security.ConvertStringSidToSid(sid_text)
        descriptor = win32security.GetNamedSecurityInfo(
            str(path),
            win32security.SE_FILE_OBJECT,
            win32security.DACL_SECURITY_INFORMATION,
        )
        dacl = descriptor.GetSecurityDescriptorDacl()
        if dacl is None:
            return
        matching = [index for index in range(dacl.GetAceCount()) if dacl.GetAce(index)[2] == sid]
        for index in reversed(matching):
            dacl.DeleteAce(index)
        if matching:
            win32security.SetNamedSecurityInfo(
                str(path),
                win32security.SE_FILE_OBJECT,
                win32security.DACL_SECURITY_INFORMATION,
                None,
                None,
                dacl,
                None,
            )
    except Exception as exc:
        raise _TransactionFailure(EXIT_ACL_FAILED, "Broker source ACL could not be removed") from exc


def _remove_service_acls(
    code_path: Path | None,
    code_boundary_path: Path | None,
    runtime_paths: Sequence[Path],
    service_executable: Path,
    service_name: str,
) -> None:
    service_sid = _service_sid(service_name)
    paths: list[Path] = []
    if code_path is not None and code_boundary_path is not None:
        paths.extend(grant.path for grant in _source_acl_grants(code_path, code_boundary_path, service_name))
    paths.extend(grant.path for grant in _runtime_acl_grants(runtime_paths, service_executable, service_name))
    for path in dict.fromkeys(paths):
        _remove_source_acl_grant(path, service_sid)


def _remove_program_data(path: Path) -> None:
    resolved = path.resolve(strict=False)
    if (
        resolved.name.casefold() != "sandboxbroker"
        or resolved.parent.name.casefold() != "mini-agent"
        or len(resolved.parts) < 3
    ):
        raise ValueError("Broker ProgramData path is outside the managed directory")
    try:
        shutil.rmtree(resolved)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise OSError("Broker ProgramData could not be removed") from exc


def _initialize_installation_key(data_path: Path) -> None:
    try:
        from .broker_service.credentials import DpapiKeyStore

        DpapiKeyStore(data_path / "installation.key.dpapi").ensure()
    except Exception as exc:
        raise _TransactionFailure(EXIT_CREDENTIAL_FAILED, "Broker installation key could not be created") from exc


def _uninstall_for_reinstall(
    service_name: str,
    service_command: Sequence[str],
    data_path: Path | None,
    code_path: Path | None,
    code_boundary_path: Path | None,
    runtime_paths: Sequence[Path],
) -> None:
    installed = _service_exists(service_name)
    if installed:
        _stop_service_for_repair(service_name)
    _remove_static_network()
    _remove_service_acls(
        code_path,
        code_boundary_path,
        runtime_paths,
        Path(service_command[0]),
        service_name,
    )
    _remove_service_rights(service_name)
    if data_path is not None:
        _remove_owned_accounts(data_path)
    if installed:
        _run(["sc.exe", "delete", service_name], failure_code=EXIT_SERVICE_FAILED)
        if not _wait_for_service_deleted(service_name):
            raise _TransactionFailure(EXIT_SERVICE_FAILED, "Broker service was not deleted")
    if data_path is not None:
        _remove_program_data(data_path)


def run_transaction(payload: Mapping[str, Any]) -> int:
    """Run one fully elevated Broker installation transaction."""

    (
        operation,
        service_name,
        service_command,
        service_class,
        backend_sid,
        sid_path,
        data_path,
        code_path,
        code_boundary_path,
        runtime_paths,
        proxy_port,
    ) = _validate_payload(payload)
    ready_path = data_path / "ready.json" if data_path is not None else None
    if ready_path is not None:
        try:
            ready_path.unlink()
        except FileNotFoundError:
            pass
    if operation == "reinstall":
        _uninstall_for_reinstall(
            service_name,
            service_command,
            data_path,
            code_path,
            code_boundary_path,
            runtime_paths,
        )
    _persist_sid(sid_path, backend_sid)
    if data_path is not None:
        _initialize_installation_key(data_path)
    if sid_path is not None and backend_sid is not None:
        # Restore a usable descriptor before touching SCM.  The service ACE is
        # added after the service exists and its virtual account is resolvable.
        _run(_sid_acl_command(sid_path, backend_sid, None), failure_code=EXIT_ACL_FAILED)
    command = subprocess.list2cmdline(list(service_command))
    if operation in {"install", "reinstall"}:
        _run(
            [
                "sc.exe",
                "create",
                service_name,
                "type=",
                "own",
                "start=",
                "demand",
                "obj=",
                f"NT SERVICE\\{service_name}",
                "binPath=",
                command,
            ]
        )
        if service_class is not None:
            _run(_service_class_command(service_name, service_class))
        _run(["sc.exe", "sidtype", service_name, "unrestricted"])
    else:
        _stop_service_for_repair(service_name)
        _run(
            [
                "sc.exe",
                "config",
                service_name,
                "type=",
                "own",
                "start=",
                "demand",
                "obj=",
                f"NT SERVICE\\{service_name}",
                "binPath=",
                command,
            ]
        )
        if service_class is not None:
            _run(_service_class_command(service_name, service_class))
        _run(["sc.exe", "sidtype", service_name, "unrestricted"])
    ready_marker = None
    if data_path is not None:
        _, ready_marker = _provision_fixed_accounts(data_path, service_name, proxy_port)
    _secure_program_data(data_path, sid_path, service_name)
    _secure_source_code(code_path, code_boundary_path, service_name)
    for grant in _runtime_acl_grants(runtime_paths, Path(service_command[0]), service_name):
        _apply_source_acl_grant(grant)
    if ready_path is not None and ready_marker is not None:
        from .broker_service.readiness import write_ready_marker

        write_ready_marker(ready_path, ready_marker)
        # os.replace preserves the temporary file's descriptor.  Apply the
        # explicit non-sensitive marker ACL after the atomic replacement so a
        # stale or token-default DACL cannot lock the normal backend out.
        if backend_sid is None:
            raise _TransactionFailure(EXIT_ACL_FAILED, "Broker backend SID is unavailable")
        for command in _managed_file_acl_commands(ready_path, backend_sid, service_name):
            _run(command, failure_code=EXIT_ACL_FAILED)
    try:
        _run(
            ["sc.exe", "start", service_name],
            failure_code=EXIT_SERVICE_START_FAILED,
            accepted_returncodes=frozenset({0, 1056}),
        )
    except Exception:
        if ready_path is not None:
            try:
                ready_path.unlink()
            except FileNotFoundError:
                pass
        raise
    return EXIT_OK


def _decode_payload(value: str) -> dict[str, Any]:
    raw = base64.urlsafe_b64decode(value.encode("ascii"))
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Broker installation payload must be an object")
    return payload


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        return EXIT_INVALID
    try:
        return run_transaction(_decode_payload(args[0]))
    except (ValueError, TypeError, json.JSONDecodeError, UnicodeError):
        return EXIT_INVALID
    except OSError:
        return EXIT_FILESYSTEM_FAILED
    except _TransactionFailure as exc:
        return exc.exit_code


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "EXIT_ACCOUNT_FAILED",
    "EXIT_ACL_FAILED",
    "EXIT_CREDENTIAL_FAILED",
    "EXIT_FILESYSTEM_FAILED",
    "EXIT_INVALID",
    "EXIT_OK",
    "EXIT_NETWORK_FAILED",
    "EXIT_RIGHTS_FAILED",
    "EXIT_SERVICE_FAILED",
    "EXIT_SERVICE_START_FAILED",
    "EXIT_SERVICE_STOP_FAILED",
    "main",
    "run_transaction",
    "_runtime_acl_grants",
]
