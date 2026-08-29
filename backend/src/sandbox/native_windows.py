"""Lazy pywin32 primitives used only inside the Windows Broker service."""

from __future__ import annotations

import ctypes
import ipaddress
import os
import re
import secrets
import subprocess
from base64 import b64encode
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import SandboxInitializationError
from .policy import FileAccessMode, NetworkMode, ResourceLimits


def _require_windows() -> None:
    if os.name != "nt":
        raise SandboxInitializationError("native sandbox security is available only on Windows")


def _modules() -> dict[str, Any]:
    _require_windows()
    try:
        import ntsecuritycon  # type: ignore[import-not-found]
        import pywintypes  # type: ignore[import-not-found]
        import win32api  # type: ignore[import-not-found]
        import win32con  # type: ignore[import-not-found]
        import win32event  # type: ignore[import-not-found]
        import win32file  # type: ignore[import-not-found]
        import win32job  # type: ignore[import-not-found]
        import win32net  # type: ignore[import-not-found]
        import win32netcon  # type: ignore[import-not-found]
        import win32pipe  # type: ignore[import-not-found]
        import win32process  # type: ignore[import-not-found]
        import win32security  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - platform dependency
        raise SandboxInitializationError("pywin32 is required by the Windows Sandbox Broker") from exc
    return {
        "api": win32api,
        "con": win32con,
        "event": win32event,
        "file": win32file,
        "job": win32job,
        "net": win32net,
        "netcon": win32netcon,
        "pipe": win32pipe,
        "process": win32process,
        "security": win32security,
        "ntsecuritycon": ntsecuritycon,
        "types": pywintypes,
    }


@dataclass(frozen=True, slots=True)
class WindowsSandboxAccount:
    name: str
    sid: str
    password: str


class WindowsAccountManager:
    """Provision non-admin local accounts for bounded per-user pools."""

    def create(self, name: str) -> WindowsSandboxAccount:
        if (
            not name
            or len(name) > 20
            or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789-_" for character in name.lower())
        ):
            raise SandboxInitializationError("sandbox account name is invalid")
        modules = _modules()
        password = secrets.token_urlsafe(32)
        try:
            try:
                modules["net"].NetUserAdd(
                    None,
                    1,
                    {
                        "name": name,
                        "password": password,
                        "priv": modules["netcon"].USER_PRIV_USER,
                        "flags": (
                            modules["netcon"].UF_SCRIPT
                            | modules["netcon"].UF_DONT_EXPIRE_PASSWD
                            | modules["netcon"].UF_PASSWD_CANT_CHANGE
                        ),
                    },
                )
            except Exception as exc:
                if getattr(exc, "winerror", None) != 2224:
                    raise
                modules["net"].NetUserSetInfo(None, name, 1003, {"password": password})
            sid, _, _ = modules["security"].LookupAccountName(None, name)
        except Exception as exc:  # pragma: no cover - requires UAC
            raise SandboxInitializationError("sandbox account could not be created") from exc
        return WindowsSandboxAccount(name, modules["security"].ConvertSidToStringSid(sid), password)

    def delete(self, name: str) -> bool:
        modules = _modules()
        try:
            modules["net"].NetUserDel(None, name)
            return True
        except Exception as exc:  # pragma: no cover - requires UAC
            winerror = getattr(exc, "winerror", None)
            if winerror == getattr(modules["netcon"], "NERR_UserNotFound", 2221):
                return True
            return False


class WindowsRestrictedTokenFactory:
    """Log on a pool account and remove every removable privilege."""

    def create(self, account: WindowsSandboxAccount) -> Any:
        modules = _modules()
        try:
            token = modules["security"].LogonUser(
                account.name,
                ".",
                account.password,
                modules["con"].LOGON32_LOGON_BATCH,
                modules["con"].LOGON32_PROVIDER_DEFAULT,
            )
            return modules["security"].CreateRestrictedToken(
                token,
                modules["security"].DISABLE_MAX_PRIVILEGE,
                [],
                [],
                [],
            )
        except Exception as exc:  # pragma: no cover - requires UAC
            raise SandboxInitializationError("sandbox restricted token could not be created") from exc


class WindowsJobObject:
    """Kernel Job Object with kill-on-close, CPU, memory and process limits."""

    def __init__(self, name: str, limits: ResourceLimits) -> None:
        modules = _modules()
        self._api = modules["api"]
        self._job = modules["job"]
        try:
            self.handle = self._job.CreateJobObject(None, name)
            info = self._job.QueryInformationJobObject(
                self.handle,
                self._job.JobObjectExtendedLimitInformation,
            )
            basic = dict(info.get("BasicLimitInformation") or {})
            flags = (
                self._job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
                | self._job.JOB_OBJECT_LIMIT_ACTIVE_PROCESS
                | self._job.JOB_OBJECT_LIMIT_JOB_TIME
                | self._job.JOB_OBJECT_LIMIT_JOB_MEMORY
            )
            basic.update(
                {
                    "LimitFlags": flags,
                    "ActiveProcessLimit": limits.processes,
                    "PerJobUserTimeLimit": limits.cpu_seconds * 10_000_000,
                }
            )
            info["BasicLimitInformation"] = basic
            info["JobMemoryLimit"] = limits.memory_mib * 1024 * 1024
            self._job.SetInformationJobObject(
                self.handle,
                self._job.JobObjectExtendedLimitInformation,
                info,
            )
        except Exception as exc:  # pragma: no cover - Windows kernel adapter
            raise SandboxInitializationError("sandbox Job Object could not be configured") from exc

    def assign(self, process_handle: Any) -> None:
        try:
            self._job.AssignProcessToJobObject(self.handle, process_handle)
        except Exception as exc:  # pragma: no cover - Windows kernel adapter
            raise SandboxInitializationError("sandbox process could not enter its Job Object") from exc

    def terminate(self, exit_code: int = 1) -> None:
        try:
            self._job.TerminateJobObject(self.handle, exit_code)
        except Exception:
            pass

    def usage(self) -> dict[str, int | float]:
        """Return cumulative Job Object accounting without exposing PIDs."""

        try:
            accounting = self._job.QueryInformationJobObject(
                self.handle,
                self._job.JobObjectBasicAndIoAccountingInformation,
            )
            extended = self._job.QueryInformationJobObject(
                self.handle,
                self._job.JobObjectExtendedLimitInformation,
            )
            process_ids = self._job.QueryInformationJobObject(
                self.handle,
                self._job.JobObjectBasicProcessIdList,
            )
            basic = accounting.get("BasicInfo") or accounting.get("BasicAccountingInformation") or {}
            io = accounting.get("IoInfo") or accounting.get("IoAccountingInformation") or {}
            pids = tuple(int(value) for value in process_ids if int(value) > 0)
            handles = sum(self._handle_count(pid) for pid in pids)
            total_time_100ns = int(basic.get("TotalUserTime", 0)) + int(basic.get("TotalKernelTime", 0))
            return {
                "cpu_seconds": total_time_100ns / 10_000_000,
                "memory_bytes": int(extended.get("PeakJobMemoryUsed", 0)),
                "processes": len(pids),
                "handles": handles,
                "disk_bytes": int(io.get("WriteTransferCount", 0)),
            }
        except Exception as exc:  # pragma: no cover - Windows kernel adapter
            raise OSError("sandbox Job Object usage could not be sampled") from exc

    @staticmethod
    def _handle_count(pid: int) -> int:
        process = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if not process:
            return 0
        try:
            count = ctypes.c_uint32()
            if not ctypes.windll.kernel32.GetProcessHandleCount(process, ctypes.byref(count)):
                return 0
            return int(count.value)
        finally:
            ctypes.windll.kernel32.CloseHandle(process)

    def close(self) -> None:
        try:
            self._api.CloseHandle(self.handle)
        except Exception:
            pass


class WindowsAclManager:
    """Protect a workspace DACL and return an SDDL snapshot for cleanup."""

    def protect(self, path: Path, account_sid: str, mode: FileAccessMode) -> str:
        if mode is FileAccessMode.FULL_ACCESS:
            raise SandboxInitializationError("full_access does not use sandbox ACLs")
        modules = _modules()
        security = modules["security"]
        target = Path(path).resolve(strict=True)
        if target.is_symlink():
            raise SandboxInitializationError("sandbox ACL target cannot be a reparse point")
        try:
            descriptor = security.GetNamedSecurityInfo(
                str(target),
                security.SE_FILE_OBJECT,
                security.OWNER_SECURITY_INFORMATION | security.DACL_SECURITY_INFORMATION,
            )
            snapshot = security.ConvertSecurityDescriptorToStringSecurityDescriptor(
                descriptor,
                security.SDDL_REVISION_1,
                security.OWNER_SECURITY_INFORMATION | security.DACL_SECURITY_INFORMATION,
            )
            owner = descriptor.GetSecurityDescriptorOwner()
            sandbox_sid = security.ConvertStringSidToSid(account_sid)
            system_sid = security.CreateWellKnownSid(security.WinLocalSystemSid, None)
            admins_sid = security.CreateWellKnownSid(security.WinBuiltinAdministratorsSid, None)
            acl = security.ACL()
            inheritance = modules["con"].OBJECT_INHERIT_ACE | modules["con"].CONTAINER_INHERIT_ACE
            full = modules["ntsecuritycon"].FILE_ALL_ACCESS
            read = modules["ntsecuritycon"].FILE_GENERIC_READ | modules["ntsecuritycon"].FILE_GENERIC_EXECUTE
            sandbox_rights = read
            if mode is FileAccessMode.WORKSPACE_WRITE:
                sandbox_rights |= modules["ntsecuritycon"].FILE_GENERIC_WRITE | modules["ntsecuritycon"].DELETE
            for sid in (system_sid, admins_sid, owner):
                acl.AddAccessAllowedAceEx(security.ACL_REVISION_DS, inheritance, full, sid)
            acl.AddAccessAllowedAceEx(security.ACL_REVISION_DS, inheritance, sandbox_rights, sandbox_sid)
            security.SetNamedSecurityInfo(
                str(target),
                security.SE_FILE_OBJECT,
                security.DACL_SECURITY_INFORMATION | security.PROTECTED_DACL_SECURITY_INFORMATION,
                None,
                None,
                acl,
                None,
            )
            return str(snapshot)
        except Exception as exc:  # pragma: no cover - requires UAC
            raise SandboxInitializationError("sandbox workspace ACL could not be applied") from exc

    def restore(self, path: Path, sddl: str) -> bool:
        modules = _modules()
        security = modules["security"]
        try:
            descriptor = security.ConvertStringSecurityDescriptorToSecurityDescriptor(
                sddl,
                security.SDDL_REVISION_1,
            )
            security.SetFileSecurity(
                str(path),
                security.OWNER_SECURITY_INFORMATION | security.DACL_SECURITY_INFORMATION,
                descriptor,
            )
            return True
        except Exception:  # pragma: no cover - requires UAC
            return False


def windows_pipe_security_attributes(*allowed_sid_strings: str) -> Any:
    """Create a protected named-pipe DACL for the Broker and backend user."""

    modules = _modules()
    security = modules["security"]
    try:
        attributes = modules["types"].SECURITY_ATTRIBUTES()
        descriptor = modules["types"].SECURITY_DESCRIPTOR()
        acl = security.ACL()
        full = modules["con"].GENERIC_READ | modules["con"].GENERIC_WRITE
        sids = [
            security.CreateWellKnownSid(security.WinLocalSystemSid, None),
            security.CreateWellKnownSid(security.WinBuiltinAdministratorsSid, None),
        ]
        sids.extend(security.ConvertStringSidToSid(value) for value in allowed_sid_strings if value)
        for sid in sids:
            acl.AddAccessAllowedAce(security.ACL_REVISION, full, sid)
        descriptor.SetSecurityDescriptorDacl(1, acl, 0)
        attributes.SECURITY_DESCRIPTOR = descriptor
        return attributes
    except Exception as exc:  # pragma: no cover - Windows security adapter
        raise SandboxInitializationError("Broker named-pipe ACL could not be created") from exc


def windows_service_sid(service_name: str) -> str:
    """Resolve the virtual service account SID used by a Windows service.

    The service SID must be present in the pipe DACL: Windows checks
    ``FILE_CREATE_PIPE_INSTANCE`` against the first instance's security
    descriptor when the service creates the next listener instance.
    """

    modules = _modules()
    try:
        sid, _, _ = modules["security"].LookupAccountName(None, f"NT SERVICE\\{service_name}")
        return str(modules["security"].ConvertSidToStringSid(sid))
    except Exception as exc:  # pragma: no cover - Windows-only adapter
        raise SandboxInitializationError("Broker service account SID is unavailable") from exc


class WindowsPowerShellWfpController:
    """Create account-scoped outbound rules through the NetSecurity/WFP layer."""

    _SAFE_NAME = re.compile(r"[A-Za-z0-9_-]{1,80}\Z")
    _SAFE_SID = re.compile(r"S-1-(?:\d+-)+\d+\Z")

    def __init__(self, *, runner=None, is_windows: bool | None = None) -> None:
        self.runner = runner or subprocess.run
        self.is_windows = os.name == "nt" if is_windows is None else is_windows

    def apply(
        self,
        *,
        rule_id: str,
        account_sid: str,
        mode: NetworkMode,
        endpoints: tuple[tuple[str, int], ...],
    ) -> tuple[str, ...]:
        if not self.is_windows:
            raise SandboxInitializationError("WFP network rules are available only on Windows")
        if self._SAFE_NAME.fullmatch(rule_id) is None or self._SAFE_SID.fullmatch(account_sid) is None:
            raise SandboxInitializationError("WFP resource identity is invalid")
        if mode is NetworkMode.FULL_NETWORK:
            return ()
        grouped: dict[str, set[int]] = {}
        for address, port in endpoints:
            canonical = str(ipaddress.ip_address(address))
            if isinstance(port, bool) or not 1 <= port <= 65535:
                raise SandboxInitializationError("WFP endpoint port is invalid")
            grouped.setdefault(canonical, set()).add(port)
        if mode is NetworkMode.RESTRICTED_NETWORK and not grouped:
            raise SandboxInitializationError("WFP restricted endpoints are missing")
        local_user = f"D:(A;;CC;;;{account_sid})"
        created: list[str] = []
        try:
            if mode is NetworkMode.NO_NETWORK:
                name = f"{rule_id}-block"
                self._run(
                    "New-NetFirewallRule",
                    {
                        "Name": name,
                        "DisplayName": name,
                        "Direction": "Outbound",
                        "Action": "Block",
                        "Enabled": "True",
                        "Profile": "Any",
                        "LocalUser": local_user,
                    },
                )
                created.append(name)
                return tuple(created)

            for version in (4, 6):
                outside = _address_complement(tuple(grouped), version=version)
                if outside:
                    name = f"{rule_id}-outside-v{version}"
                    self._run(
                        "New-NetFirewallRule",
                        {
                            "Name": name,
                            "DisplayName": name,
                            "Direction": "Outbound",
                            "Action": "Block",
                            "Enabled": "True",
                            "Profile": "Any",
                            "RemoteAddress": outside,
                            "LocalUser": local_user,
                        },
                    )
                    created.append(name)

            for index, (address, allowed_ports) in enumerate(sorted(grouped.items())):
                blocked_ports = _port_complement(allowed_ports)
                if blocked_ports:
                    name = f"{rule_id}-tcp-{index}"
                    self._run(
                        "New-NetFirewallRule",
                        {
                            "Name": name,
                            "DisplayName": name,
                            "Direction": "Outbound",
                            "Action": "Block",
                            "Enabled": "True",
                            "Profile": "Any",
                            "Protocol": "TCP",
                            "RemoteAddress": address,
                            "RemotePort": blocked_ports,
                            "LocalUser": local_user,
                        },
                    )
                    created.append(name)
                for protocol in ("UDP", "ICMPv4" if ipaddress.ip_address(address).version == 4 else "ICMPv6"):
                    name = f"{rule_id}-{protocol.lower()}-{index}"
                    self._run(
                        "New-NetFirewallRule",
                        {
                            "Name": name,
                            "DisplayName": name,
                            "Direction": "Outbound",
                            "Action": "Block",
                            "Enabled": "True",
                            "Profile": "Any",
                            "Protocol": protocol,
                            "RemoteAddress": address,
                            "LocalUser": local_user,
                        },
                    )
                    created.append(name)
            return tuple(created)
        except Exception:
            self.remove(tuple(created))
            raise

    def remove(self, rule_ids: tuple[str, ...]) -> bool:
        complete = True
        for name in rule_ids:
            if self._SAFE_NAME.fullmatch(name) is None:
                complete = False
                continue
            try:
                self._run("Remove-NetFirewallRule", {"Name": name}, tolerate_missing=True)
            except Exception:
                complete = False
        return complete

    def _run(
        self,
        command: str,
        values: Mapping[str, str | Sequence[str]],
        *,
        tolerate_missing: bool = False,
    ) -> None:
        arguments = " ".join(f"-{name} {_powershell_literal(value)}" for name, value in values.items())
        script = f"$ErrorActionPreference='Stop'; {command} {arguments} | Out-Null"
        encoded = b64encode(script.encode("utf-16-le")).decode("ascii")
        result = self.runner(
            ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
            check=False,
            capture_output=True,
            timeout=30.0,
        )
        if getattr(result, "returncode", 1) != 0 and not tolerate_missing:
            raise SandboxInitializationError("Broker WFP operation failed")


def _address_complement(addresses: tuple[str, ...], *, version: int) -> tuple[str, ...]:
    values = sorted({int(address) for raw in addresses if (address := ipaddress.ip_address(raw)).version == version})
    maximum = (1 << (32 if version == 4 else 128)) - 1
    if not values:
        return ("0.0.0.0/0" if version == 4 else "::/0",)
    result: list[str] = []
    start = 0
    for value in values:
        if start < value:
            result.append(_address_range(start, value - 1, version=version))
        start = value + 1
    if start <= maximum:
        result.append(_address_range(start, maximum, version=version))
    return tuple(result)


def _address_range(start: int, end: int, *, version: int) -> str:
    address_type = ipaddress.IPv4Address if version == 4 else ipaddress.IPv6Address
    first = str(address_type(start))
    last = str(address_type(end))
    return first if start == end else f"{first}-{last}"


def _port_complement(allowed: set[int]) -> tuple[str, ...]:
    result: list[str] = []
    start = 1
    for port in sorted(allowed):
        if start < port:
            result.append(str(start) if start == port - 1 else f"{start}-{port - 1}")
        start = port + 1
    if start <= 65535:
        result.append(str(start) if start == 65535 else f"{start}-65535")
    return tuple(result)


def _powershell_literal(value: str | Sequence[str]) -> str:
    def quote(item: str) -> str:
        return "'" + item.replace("'", "''") + "'"

    if isinstance(value, str):
        return quote(value)
    return "@(" + ",".join(quote(str(item)) for item in value) + ")"


__all__ = [
    "WindowsAccountManager",
    "WindowsAclManager",
    "WindowsJobObject",
    "WindowsRestrictedTokenFactory",
    "WindowsSandboxAccount",
    "WindowsPowerShellWfpController",
    "windows_pipe_security_attributes",
    "windows_service_sid",
]
