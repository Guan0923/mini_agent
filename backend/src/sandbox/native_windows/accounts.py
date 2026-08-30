"""Sandbox local-account provisioning and restricted tokens."""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import Any

from ..errors import SandboxInitializationError
from ..policy import FileAccessMode
from .api import _modules


@dataclass(frozen=True, slots=True)
class WindowsSandboxAccount:
    name: str
    sid: str
    password: str


@dataclass(frozen=True, slots=True)
class WindowsReservedToken:
    token: Any
    logon_sid: str
    account_sid: str
    workspace_cap_sid: str
    temp_cap_sid: str


class WindowsRestrictedTokenFactory:
    """Create a per-logon restricted token from one fixed sandbox account."""

    def __init__(self, service_sid: str | None = None, *, sid_factory=None) -> None:
        self.service_sid = service_sid
        self._sid_factory = sid_factory or random_capability_sid

    def reserve(self, account: WindowsSandboxAccount, file_mode: FileAccessMode) -> WindowsReservedToken:
        modules = _modules()
        source = None
        workspace_cap_sid = self._sid_factory()
        temp_cap_sid = self._sid_factory()
        if workspace_cap_sid == temp_cap_sid:
            raise SandboxInitializationError("sandbox capability SID collision")
        try:
            security = modules["security"]
            try:
                source = security.LogonUser(
                    account.name,
                    ".",
                    account.password,
                    modules["con"].LOGON32_LOGON_BATCH,
                    modules["con"].LOGON32_PROVIDER_DEFAULT,
                )
            except Exception as exc:  # pragma: no cover - requires Windows account
                raise SandboxInitializationError("sandbox account batch logon failed") from exc
            try:
                logon_sid = self._logon_sid(source)
                logon_sid_value = security.ConvertStringSidToSid(logon_sid)
            except Exception as exc:  # pragma: no cover - requires Windows token
                raise SandboxInitializationError("sandbox logon SID extraction failed") from exc
            capability_values = (
                security.ConvertStringSidToSid(workspace_cap_sid),
                security.ConvertStringSidToSid(temp_cap_sid),
            )
            flags = security.DISABLE_MAX_PRIVILEGE | 0x4  # LUA_TOKEN
            restricting_sids: list[tuple[Any, int]] = []
            if file_mode is not FileAccessMode.FULL_ACCESS:
                # Limit the restricting-SID access check to write access. Read
                # and execute continue to use the low-privilege account's
                # ordinary groups (for example BUILTIN\Users).
                flags |= 0x8  # WRITE_RESTRICTED
                account_sid_value = security.ConvertStringSidToSid(account.sid)
                everyone_sid = security.CreateWellKnownSid(security.WinWorldSid, None)
                restricting_sids.extend(
                    (
                        (capability_values[0], 0),
                        (capability_values[1], 0),
                        (account_sid_value, 0),
                        (logon_sid_value, 0),
                        (everyone_sid, 0),
                    )
                )
            try:
                token = security.CreateRestrictedToken(source, flags, [], [], restricting_sids)
            except Exception as exc:  # pragma: no cover - requires Windows token
                raise SandboxInitializationError("sandbox restricted token creation failed") from exc
            try:
                self._set_default_dacl(token, logon_sid_value, capability_values, self.service_sid)
                self._restore_change_notify(token)
            except Exception as exc:  # pragma: no cover - requires Windows token
                try:
                    token.Close()
                except Exception:
                    pass
                raise SandboxInitializationError("sandbox token default DACL configuration failed") from exc
            return WindowsReservedToken(
                token,
                logon_sid,
                account.sid,
                workspace_cap_sid,
                temp_cap_sid,
            )
        finally:
            if source is not None:
                try:
                    source.Close()
                except Exception:
                    pass

    @staticmethod
    def _logon_sid(token: Any) -> str:
        modules = _modules()
        security = modules["security"]
        for sid, attributes in security.GetTokenInformation(token, security.TokenGroups):
            if int(attributes) & 0xC0000000 == 0xC0000000:
                value = str(security.ConvertSidToStringSid(sid))
                if value.startswith("S-1-5-5-"):
                    return value
        raise SandboxInitializationError("sandbox logon SID is unavailable")

    @staticmethod
    def _set_default_dacl(
        token: Any,
        logon_sid: Any,
        capability_sids: tuple[Any, Any],
        service_sid: str | None,
    ) -> None:
        modules = _modules()
        security = modules["security"]
        acl = security.ACL()
        full = modules["con"].GENERIC_ALL
        acl.AddAccessAllowedAce(security.ACL_REVISION, full, logon_sid)
        for capability_sid in capability_sids:
            acl.AddAccessAllowedAce(security.ACL_REVISION, full, capability_sid)
        acl.AddAccessAllowedAce(
            security.ACL_REVISION, full, security.CreateWellKnownSid(security.WinLocalSystemSid, None)
        )
        if service_sid:
            acl.AddAccessAllowedAce(security.ACL_REVISION, full, security.ConvertStringSidToSid(service_sid))
        security.SetTokenInformation(token, security.TokenDefaultDacl, acl)

    @staticmethod
    def _restore_change_notify(token: Any) -> None:
        modules = _modules()
        security = modules["security"]
        luid = security.LookupPrivilegeValue(None, "SeChangeNotifyPrivilege")
        security.AdjustTokenPrivileges(token, False, [(luid, security.SE_PRIVILEGE_ENABLED)])


def random_capability_sid() -> str:
    """Create a private ordinary SID without registering an account or group."""

    parts = tuple(secrets.randbits(32) or 1 for _ in range(4))
    return "S-1-5-21-" + "-".join(str(value) for value in parts)
