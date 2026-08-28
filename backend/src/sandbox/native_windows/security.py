"""Workspace ACL and Broker named-pipe security descriptors."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..errors import SandboxInitializationError
from ..policy import FileAccessMode
from .api import _modules


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
