"""Workspace ACL and Broker named-pipe security descriptors."""

from __future__ import annotations

import ctypes
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..errors import SandboxInitializationError
from ..policy import FileAccessMode
from .api import _modules


class WindowsAclManager:
    """Manage per-logon-SID ACE leases without replacing an existing DACL."""

    def grant_lease(self, path: Path, logon_sid: str, mode: FileAccessMode) -> None:
        rights = _modules()["ntsecuritycon"].FILE_GENERIC_READ | _modules()["ntsecuritycon"].FILE_GENERIC_EXECUTE
        if mode is not FileAccessMode.READ_ONLY:
            rights |= _modules()["ntsecuritycon"].FILE_GENERIC_WRITE | _modules()["ntsecuritycon"].DELETE
        self._grant(path, logon_sid, rights)

    def grant_execute_lease(self, path: Path, sid_value: str) -> None:
        """Grant the temporary image/cwd access CreateProcessAsUser needs."""

        nt = _modules()["ntsecuritycon"]
        self._grant(path, sid_value, nt.FILE_GENERIC_READ | nt.FILE_GENERIC_EXECUTE)

    def grant_capability_write(self, path: Path, sid_value: str) -> None:
        modules = _modules()
        rights = modules["ntsecuritycon"].FILE_GENERIC_READ | modules["ntsecuritycon"].FILE_GENERIC_EXECUTE
        rights |= modules["ntsecuritycon"].FILE_GENERIC_WRITE | modules["ntsecuritycon"].DELETE
        self._grant(path, sid_value, rights)

    def deny_capability_write(self, path: Path, sid_value: str) -> None:
        modules = _modules()
        nt = modules["ntsecuritycon"]
        mask = nt.FILE_GENERIC_WRITE | nt.DELETE | nt.FILE_DELETE_CHILD
        try:
            _add_native_deny_ace(Path(path), sid_value, mask)
        except Exception as exc:  # pragma: no cover - requires Windows ACL support
            raise SandboxInitializationError("sandbox capability deny ACL could not be applied") from exc

    def path_allows_write(self, path: Path, *sid_values: str) -> bool:
        """Conservatively detect effective write allows for the named SIDs."""

        modules = _modules()
        security = modules["security"]
        nt = modules["ntsecuritycon"]
        target = Path(path)
        try:
            descriptor = security.GetNamedSecurityInfo(
                str(target),
                security.SE_FILE_OBJECT,
                security.DACL_SECURITY_INFORMATION,
            )
            dacl = descriptor.GetSecurityDescriptorDacl()
            if dacl is None:
                return True
            sids = tuple(security.ConvertStringSidToSid(value) for value in sid_values)
            write_mask = (
                nt.FILE_GENERIC_WRITE
                | nt.DELETE
                | nt.FILE_DELETE_CHILD
                | modules["con"].GENERIC_WRITE
                | modules["con"].GENERIC_ALL
                | modules["con"].WRITE_DAC
                | modules["con"].WRITE_OWNER
            )
            denied = 0
            allowed = 0
            for index in range(dacl.GetAceCount()):
                ace = dacl.GetAce(index)
                ace_type = ace[0][0]
                if len(ace) < 3 or ace[2] not in sids:
                    continue
                mask = int(ace[1]) & write_mask
                if ace_type == security.ACCESS_DENIED_ACE_TYPE:
                    denied |= mask
                elif ace_type == security.ACCESS_ALLOWED_ACE_TYPE:
                    allowed |= mask & ~denied
                else:
                    raise SandboxInitializationError("sandbox ACL contains an unsupported matching ACE")
            return bool(allowed)
        except SandboxInitializationError:
            raise
        except Exception as exc:  # pragma: no cover - requires Windows ACL support
            raise SandboxInitializationError("sandbox path ACL could not be inspected") from exc

    def capability_write_denied(self, path: Path, sid_value: str) -> bool:
        modules = _modules()
        security = modules["security"]
        nt = modules["ntsecuritycon"]
        mask = nt.FILE_GENERIC_WRITE | nt.DELETE | nt.FILE_DELETE_CHILD
        try:
            descriptor = security.GetNamedSecurityInfo(
                str(path),
                security.SE_FILE_OBJECT,
                security.DACL_SECURITY_INFORMATION,
            )
            dacl = descriptor.GetSecurityDescriptorDacl()
            if dacl is None:
                return False
            sid = security.ConvertStringSidToSid(sid_value)
            return any(
                (ace := dacl.GetAce(index))[0][0] == security.ACCESS_DENIED_ACE_TYPE
                and ace[2] == sid
                and int(ace[1]) & mask == mask
                for index in range(dacl.GetAceCount())
            )
        except Exception as exc:  # pragma: no cover - requires Windows ACL support
            raise SandboxInitializationError("sandbox capability deny ACL could not be verified") from exc

    def path_identity(self, path: Path) -> PathIdentity:
        return _path_identity(Path(path))

    @staticmethod
    def _grant(path: Path, sid_value: str, rights: int) -> None:
        modules = _modules()
        security = modules["security"]
        target = Path(path).resolve(strict=True)
        if target.is_symlink():
            raise SandboxInitializationError("sandbox ACL target cannot be a reparse point")
        try:
            descriptor = security.GetNamedSecurityInfo(
                str(target),
                security.SE_FILE_OBJECT,
                security.DACL_SECURITY_INFORMATION,
            )
            dacl = descriptor.GetSecurityDescriptorDacl() or security.ACL()
            sid = security.ConvertStringSidToSid(sid_value)
            inheritance = modules["con"].OBJECT_INHERIT_ACE | modules["con"].CONTAINER_INHERIT_ACE
            for index in range(dacl.GetAceCount()):
                ace = dacl.GetAce(index)
                if ace[0][0] == security.ACCESS_ALLOWED_ACE_TYPE and ace[2] == sid and ace[1] & rights == rights:
                    return
            dacl.AddAccessAllowedAceEx(security.ACL_REVISION_DS, inheritance, rights, sid)
            security.SetNamedSecurityInfo(
                str(target),
                security.SE_FILE_OBJECT,
                security.DACL_SECURITY_INFORMATION,
                None,
                None,
                dacl,
                None,
            )
        except Exception as exc:  # pragma: no cover - requires Windows ACL support
            raise SandboxInitializationError("sandbox ACL lease could not be applied") from exc

    def revoke_lease(self, path: Path, logon_sid: str) -> bool:
        modules = _modules()
        security = modules["security"]
        try:
            target = Path(path).resolve(strict=True)
            descriptor = security.GetNamedSecurityInfo(
                str(target),
                security.SE_FILE_OBJECT,
                security.DACL_SECURITY_INFORMATION,
            )
            dacl = descriptor.GetSecurityDescriptorDacl()
            if dacl is None:
                return True
            sid = security.ConvertStringSidToSid(logon_sid)
            matches = [
                index
                for index in range(dacl.GetAceCount())
                if (ace := dacl.GetAce(index))[0][0]
                in {security.ACCESS_ALLOWED_ACE_TYPE, security.ACCESS_DENIED_ACE_TYPE}
                and ace[2] == sid
            ]
            for index in reversed(matches):
                dacl.DeleteAce(index)
            if matches:
                security.SetNamedSecurityInfo(
                    str(target),
                    security.SE_FILE_OBJECT,
                    security.DACL_SECURITY_INFORMATION,
                    None,
                    None,
                    dacl,
                    None,
                )
            return True
        except Exception:  # pragma: no cover - requires Windows ACL support
            return False


@dataclass(frozen=True, slots=True)
class PathIdentity:
    path: Path
    object_id: str


def _path_identity(path: Path, _seen: set[str] | None = None) -> PathIdentity:
    modules = _modules()
    con = modules["con"]
    handle = None
    seen = set() if _seen is None else _seen
    lexical = os.path.normcase(os.path.abspath(str(path)))
    if lexical in seen:
        raise SandboxInitializationError("sandbox reparse path contains a loop")
    seen.add(lexical)
    try:
        if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
            raw_target = os.readlink(path)
            normalized_target = raw_target.removeprefix("\\\\?\\")
            return _path_identity(Path(normalized_target), seen)
        handle = modules["file"].CreateFile(
            str(path),
            0,
            con.FILE_SHARE_READ | con.FILE_SHARE_WRITE | con.FILE_SHARE_DELETE,
            None,
            con.OPEN_EXISTING,
            con.FILE_FLAG_BACKUP_SEMANTICS,
            None,
        )
        final = modules["file"].GetFinalPathNameByHandle(handle, 0)
        info = modules["file"].GetFileInformationByHandle(handle)
        if not isinstance(final, str) or len(info) < 10:
            raise OSError("invalid file identity")
        normalized = final.removeprefix("\\\\?\\")
        object_id = f"{int(info[4]):08x}:{int(info[8]):08x}:{int(info[9]):08x}"
        return PathIdentity(Path(os.path.normcase(normalized)), object_id)
    except SandboxInitializationError:
        raise
    except Exception as exc:  # pragma: no cover - requires Windows handle APIs
        raise SandboxInitializationError("sandbox path identity could not be resolved") from exc
    finally:
        if handle is not None:
            try:
                handle.Close()
            except Exception:
                pass


class _TrusteeW(ctypes.Structure):
    _fields_ = [
        ("pMultipleTrustee", ctypes.c_void_p),
        ("MultipleTrusteeOperation", ctypes.c_int),
        ("TrusteeForm", ctypes.c_int),
        ("TrusteeType", ctypes.c_int),
        ("ptstrName", ctypes.c_void_p),
    ]


class _ExplicitAccessW(ctypes.Structure):
    _fields_ = [
        ("grfAccessPermissions", ctypes.c_uint32),
        ("grfAccessMode", ctypes.c_int),
        ("grfInheritance", ctypes.c_uint32),
        ("Trustee", _TrusteeW),
    ]


def _add_native_deny_ace(path: Path, sid_value: str, mask: int) -> None:
    """Use SetEntriesInAcl so the new deny ACE is placed before allow ACEs."""

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    sid = ctypes.c_void_p()
    descriptor = ctypes.c_void_p()
    old_dacl = ctypes.c_void_p()
    new_dacl = ctypes.c_void_p()
    convert = advapi.ConvertStringSidToSidW
    convert.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_void_p)]
    convert.restype = ctypes.c_int
    if not convert(sid_value, ctypes.byref(sid)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        get_info = advapi.GetNamedSecurityInfoW
        get_info.argtypes = [
            ctypes.c_wchar_p,
            ctypes.c_int,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        get_info.restype = ctypes.c_uint32
        code = get_info(str(path), 1, 0x4, None, None, ctypes.byref(old_dacl), None, ctypes.byref(descriptor))
        if code:
            raise OSError(code, "GetNamedSecurityInfoW failed")
        entry = _ExplicitAccessW(
            int(mask),
            3,  # DENY_ACCESS
            0x1 | 0x2,  # OBJECT_INHERIT_ACE | CONTAINER_INHERIT_ACE
            _TrusteeW(None, 0, 0, 0, sid),
        )
        set_entries = advapi.SetEntriesInAclW
        set_entries.argtypes = [
            ctypes.c_ulong,
            ctypes.POINTER(_ExplicitAccessW),
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        set_entries.restype = ctypes.c_uint32
        code = set_entries(1, ctypes.byref(entry), old_dacl, ctypes.byref(new_dacl))
        if code:
            raise OSError(code, "SetEntriesInAclW failed")
        set_info = advapi.SetNamedSecurityInfoW
        set_info.argtypes = [
            ctypes.c_wchar_p,
            ctypes.c_int,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        set_info.restype = ctypes.c_uint32
        code = set_info(str(path), 1, 0x4, None, None, new_dacl, None)
        if code:
            raise OSError(code, "SetNamedSecurityInfoW failed")
    finally:
        for value in (new_dacl, descriptor, sid):
            if value.value:
                kernel.LocalFree(value)


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
