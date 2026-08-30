from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.sandbox import FileAccessMode
from backend.sandbox.native_windows import WindowsRestrictedTokenFactory, WindowsSandboxAccount


class _Handle:
    def __init__(self) -> None:
        self.closed = False

    def Close(self) -> None:
        self.closed = True


class _Acl:
    def __init__(self) -> None:
        self.allowed: list[tuple[int, object]] = []

    def AddAccessAllowedAce(self, _revision: int, rights: int, sid: object) -> None:
        self.allowed.append((rights, sid))


class _Security:
    DISABLE_MAX_PRIVILEGE = 0x1
    SE_PRIVILEGE_ENABLED = 0x2
    ACL_REVISION = 2
    TokenGroups = 1
    TokenDefaultDacl = 2
    WinWorldSid = 1
    WinLocalSystemSid = 22

    def __init__(self) -> None:
        self.restricted_calls: list[tuple[int, list[tuple[object, int]]]] = []
        self.default_dacl: _Acl | None = None
        self.adjusted: list[tuple[object, bool, list[tuple[object, int]]]] = []

    def LogonUser(self, *_args) -> _Handle:
        return _Handle()

    def GetTokenInformation(self, _token, _kind):
        return [("S-1-5-5-10-20", 0xC0000000)]

    @staticmethod
    def ConvertSidToStringSid(sid) -> str:
        return str(sid)

    @staticmethod
    def ConvertStringSidToSid(sid: str) -> str:
        return sid

    @staticmethod
    def CreateWellKnownSid(kind: int, _domain) -> str:
        return "S-1-1-0" if kind == 1 else "S-1-5-18"

    def CreateRestrictedToken(self, _source, flags, _disabled, _deleted, restricting):
        self.restricted_calls.append((flags, list(restricting)))
        return _Handle()

    @staticmethod
    def ACL() -> _Acl:
        return _Acl()

    def SetTokenInformation(self, _token, _kind, acl: _Acl) -> None:
        self.default_dacl = acl

    @staticmethod
    def LookupPrivilegeValue(_system, name: str) -> str:
        return name

    def AdjustTokenPrivileges(self, token, disable_all: bool, privileges) -> None:
        self.adjusted.append((token, disable_all, privileges))


@pytest.mark.parametrize(
    ("file_mode", "expected_flags", "expected_restricting"),
    [
        (
            FileAccessMode.READ_ONLY,
            0x1 | 0x4,
            ["workspace-cap", "temp-cap", "account", "S-1-5-5-10-20", "S-1-1-0"],
        ),
        (
            FileAccessMode.WORKSPACE_WRITE,
            0x1 | 0x4,
            ["workspace-cap", "temp-cap", "account", "S-1-5-5-10-20", "S-1-1-0"],
        ),
        (FileAccessMode.FULL_ACCESS, 0x1 | 0x4, []),
    ],
)
def test_restricted_token_uses_exact_capability_model(
    monkeypatch: pytest.MonkeyPatch,
    file_mode: FileAccessMode,
    expected_flags: int,
    expected_restricting: list[str],
) -> None:
    security = _Security()
    modules = {
        "security": security,
        "con": SimpleNamespace(
            LOGON32_LOGON_BATCH=4,
            LOGON32_PROVIDER_DEFAULT=0,
            GENERIC_ALL=0x10000000,
        ),
    }
    monkeypatch.setattr("backend.sandbox.native_windows.accounts._modules", lambda: modules)
    values = iter(("workspace-cap", "temp-cap"))
    factory = WindowsRestrictedTokenFactory("service", sid_factory=lambda: next(values))

    reserved = factory.reserve(WindowsSandboxAccount("sandbox", "account", "password"), file_mode)

    flags, restricting = security.restricted_calls[-1]
    assert flags == expected_flags
    assert [sid for sid, attributes in restricting if attributes == 0] == expected_restricting
    assert reserved.workspace_cap_sid == "workspace-cap"
    assert reserved.temp_cap_sid == "temp-cap"
    assert security.default_dacl is not None
    default_sids = [sid for _rights, sid in security.default_dacl.allowed]
    assert default_sids == ["S-1-5-5-10-20", "workspace-cap", "temp-cap", "S-1-5-18", "service"]
    assert "S-1-1-0" not in default_sids
    assert security.adjusted[-1][1:] == (False, [("SeChangeNotifyPrivilege", security.SE_PRIVILEGE_ENABLED)])
