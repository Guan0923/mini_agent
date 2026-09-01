"""Windows sandbox account, credential, and logon-right lifecycle."""

from __future__ import annotations

import os
import secrets
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from .access_policy import _service_sid
from .contracts import (
    EXIT_ACCOUNT_FAILED,
    EXIT_CREDENTIAL_FAILED,
    EXIT_NETWORK_FAILED,
    EXIT_RIGHTS_FAILED,
    TransactionFailure,
)

OFFLINE_ACCOUNT = "MiniSbxOffline"
ONLINE_ACCOUNT = "MiniSbxOnline"
ACCOUNT_GROUP = "MiniAgentSandboxUsers"
ACCOUNT_COMMENT = "Mini-Agent sandbox account (managed)"
GROUP_COMMENT = "Mini-Agent sandbox users (managed)"


def provision_fixed_accounts(
    data_path: Path,
    service_name: str,
    proxy_port: int,
    *,
    configure_network: Callable[[str, str, int], None],
):
    """Create or validate the two managed identities and persist credentials."""

    try:
        import win32con  # type: ignore[import-not-found]
        import win32net  # type: ignore[import-not-found]
        import win32netcon  # type: ignore[import-not-found]
        import win32security  # type: ignore[import-not-found]

        from ..broker_service.credentials import BrokerCredentialPackage, DpapiCredentialStore
        from ..broker_service.readiness import build_ready_marker
    except ImportError as exc:
        raise OSError("Broker account dependencies are unavailable") from exc

    group_name = ACCOUNT_GROUP
    try:
        win32net.NetLocalGroupAdd(None, 1, {"name": group_name, "comment": GROUP_COMMENT})
    except Exception as exc:
        if getattr(exc, "winerror", None) not in {1379, 2223}:
            raise TransactionFailure(EXIT_ACCOUNT_FAILED, "Broker sandbox group could not be created") from exc
        try:
            group_info = win32net.NetLocalGroupGetInfo(None, group_name, 1)
        except Exception as read_exc:
            raise TransactionFailure(EXIT_ACCOUNT_FAILED, "Broker sandbox group is unavailable") from read_exc
        if str(group_info.get("comment") or "") != GROUP_COMMENT:
            raise TransactionFailure(EXIT_ACCOUNT_FAILED, "Conflicting sandbox group ownership")

    credential_store = DpapiCredentialStore(data_path / "accounts.dpapi")
    try:
        existing_package = credential_store.load()
    except Exception:
        existing_package = None

    accounts: dict[str, tuple[str, str]] = {}
    for role, name in (("offline", OFFLINE_ACCOUNT), ("online", ONLINE_ACCOUNT)):
        password = (
            getattr(existing_package, f"{role}_password", "")
            if existing_package is not None and getattr(existing_package, f"{role}_name", "") == name
            else ""
        )
        try:
            info = win32net.NetUserGetInfo(None, name, 4)
            validate_existing_sandbox_user(name, info, win32net)
        except Exception as exc:
            if getattr(exc, "winerror", None) != 2221:
                if isinstance(exc, TransactionFailure):
                    raise
                raise TransactionFailure(EXIT_ACCOUNT_FAILED, "Conflicting sandbox account exists") from exc
            password = secrets_token()
            try:
                win32net.NetUserAdd(
                    None,
                    1,
                    {
                        "name": name,
                        "password": password,
                        # Level 1 rejects USER_PRIV_GUEST. Without membership
                        # in the built-in Users group Windows persists it as a
                        # guest-privilege account, so request USER explicitly.
                        "priv": win32netcon.USER_PRIV_USER,
                        "flags": (
                            win32netcon.UF_SCRIPT
                            | win32netcon.UF_DONT_EXPIRE_PASSWD
                            | win32netcon.UF_PASSWD_CANT_CHANGE
                        ),
                        "comment": ACCOUNT_COMMENT,
                    },
                )
            except Exception as create_exc:
                raise TransactionFailure(
                    EXIT_ACCOUNT_FAILED, "Broker sandbox account could not be created"
                ) from create_exc
        try:
            sid, _, _ = win32security.LookupAccountName(None, name)
        except Exception as exc:
            raise TransactionFailure(EXIT_ACCOUNT_FAILED, "Broker sandbox account SID is unavailable") from exc
        sid_text = str(win32security.ConvertSidToStringSid(sid))
        package_sid = getattr(existing_package, f"{role}_sid", "") if existing_package is not None else ""
        if not password or package_sid != sid_text or not credential_works(name, password, win32security, win32con):
            password = secrets_token()
            try:
                win32net.NetUserSetInfo(None, name, 1003, {"password": password})
            except Exception as exc:
                raise TransactionFailure(
                    EXIT_CREDENTIAL_FAILED, "Broker sandbox credential could not be rotated"
                ) from exc
        try:
            info = win32net.NetUserGetInfo(None, name, 4)
            flags = int(info.get("flags", 0)) | win32netcon.UF_DONT_EXPIRE_PASSWD | win32netcon.UF_PASSWD_CANT_CHANGE
            win32net.NetUserSetInfo(None, name, 1008, {"flags": flags})
        except Exception as exc:
            raise TransactionFailure(EXIT_ACCOUNT_FAILED, "Broker sandbox account flags could not be set") from exc
        try:
            win32net.NetLocalGroupAddMembers(None, group_name, 3, [{"domainandname": name}])
        except Exception as exc:
            if getattr(exc, "winerror", None) != 1378:
                raise TransactionFailure(EXIT_ACCOUNT_FAILED, "Broker sandbox group membership failed") from exc
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
        raise TransactionFailure(EXIT_RIGHTS_FAILED, "Broker logon rights could not be configured") from exc
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
        OFFLINE_ACCOUNT,
        accounts["offline"][0],
        accounts["offline"][1],
        ONLINE_ACCOUNT,
        accounts["online"][0],
        accounts["online"][1],
    )
    try:
        credential_store.save(package)
    except Exception as exc:
        raise TransactionFailure(EXIT_CREDENTIAL_FAILED, "Broker credentials could not be persisted") from exc
    try:
        configure_network(accounts["offline"][0], accounts["online"][0], proxy_port)
    except Exception as exc:
        raise TransactionFailure(EXIT_NETWORK_FAILED, "Broker network policy could not be configured") from exc
    return package, build_ready_marker(package, proxy_port)


def validate_existing_sandbox_user(name: str, info: Mapping[str, Any], win32net: Any) -> None:
    if int(info.get("priv", -1)) != 0:
        raise TransactionFailure(EXIT_ACCOUNT_FAILED, "Conflicting sandbox account privilege")
    forbidden = {"administrators", "backup operators", "power users", "remote desktop users"}
    groups = {str(value).casefold() for value in win32net.NetUserGetLocalGroups(None, name, 0)}
    if groups & forbidden:
        raise TransactionFailure(EXIT_ACCOUNT_FAILED, "Conflicting sandbox account group membership")
    if str(info.get("comment") or "") != ACCOUNT_COMMENT:
        raise TransactionFailure(EXIT_ACCOUNT_FAILED, "Conflicting sandbox account ownership")


def credential_works(name: str, password: str, security: Any, win32con: Any) -> bool:
    try:
        token = security.LogonUser(name, ".", password, win32con.LOGON32_LOGON_BATCH, win32con.LOGON32_PROVIDER_DEFAULT)
        token.Close()
        return True
    except Exception:
        return False


def remove_owned_accounts(data_path: Path) -> None:
    """Delete only accounts proven to belong to this Mini-Agent install."""

    try:
        import win32net  # type: ignore[import-not-found]
        import win32security  # type: ignore[import-not-found]

        from ..broker_service.credentials import DpapiCredentialStore
    except ImportError as exc:
        raise OSError("Broker account dependencies are unavailable") from exc
    try:
        package = DpapiCredentialStore(data_path / "accounts.dpapi").load()
    except Exception:
        if managed_identity_exists(win32net):
            raise TransactionFailure(EXIT_ACCOUNT_FAILED, "Broker sandbox account ownership is unverified")
        return
    expected = {
        OFFLINE_ACCOUNT: package.offline_sid if package.offline_name == OFFLINE_ACCOUNT else None,
        ONLINE_ACCOUNT: package.online_sid if package.online_name == ONLINE_ACCOUNT else None,
    }
    if any(value is None for value in expected.values()):
        if managed_identity_exists(win32net):
            raise TransactionFailure(EXIT_ACCOUNT_FAILED, "Broker sandbox account ownership is unverified")
        return
    try:
        group_info = win32net.NetLocalGroupGetInfo(None, ACCOUNT_GROUP, 1)
        raw_members = win32net.NetLocalGroupGetMembers(None, ACCOUNT_GROUP, 3)[0]
        members = {
            str(value.get("domainandname") or "").casefold() for value in raw_members if isinstance(value, Mapping)
        }
    except Exception as exc:
        if getattr(exc, "winerror", None) in {1376, 2220}:
            if managed_identity_exists(win32net):
                raise TransactionFailure(EXIT_ACCOUNT_FAILED, "Broker sandbox account ownership is unverified")
            return
        raise TransactionFailure(EXIT_ACCOUNT_FAILED, "Broker sandbox group could not be verified") from exc
    if str(group_info.get("comment") or "") != GROUP_COMMENT:
        raise TransactionFailure(EXIT_ACCOUNT_FAILED, "Conflicting sandbox group ownership")
    expected_members = {f"{os.environ.get('COMPUTERNAME', '.')}\\{name}".casefold() for name in expected}
    if members != expected_members and {member.rsplit("\\", 1)[-1] for member in members} != {
        name.casefold() for name in expected
    }:
        raise TransactionFailure(EXIT_ACCOUNT_FAILED, "Conflicting sandbox group membership")
    try:
        policy_handle = win32security.LsaOpenPolicy(None, win32security.POLICY_ALL_ACCESS)
        group_sid, _, _ = win32security.LookupAccountName(None, ACCOUNT_GROUP)
        try:
            win32security.LsaRemoveAccountRights(policy_handle, group_sid, True, ())
        except Exception as exc:
            if getattr(exc, "winerror", None) not in {2, 1332}:
                raise
        for name, expected_sid in expected.items():
            info = win32net.NetUserGetInfo(None, name, 4)
            validate_existing_sandbox_user(name, info, win32net)
            sid, _, _ = win32security.LookupAccountName(None, name)
            sid_text = str(win32security.ConvertSidToStringSid(sid))
            if str(info.get("comment") or "") != ACCOUNT_COMMENT or sid_text != expected_sid:
                raise TransactionFailure(EXIT_ACCOUNT_FAILED, "Conflicting sandbox account ownership")
        for name in expected:
            win32net.NetUserDel(None, name)
        win32net.NetLocalGroupDel(None, ACCOUNT_GROUP)
    except TransactionFailure:
        raise
    except Exception as exc:
        raise TransactionFailure(EXIT_ACCOUNT_FAILED, "Broker sandbox accounts could not be removed") from exc


def managed_identity_exists(win32net: Any) -> bool:
    for name in (OFFLINE_ACCOUNT, ONLINE_ACCOUNT):
        try:
            win32net.NetUserGetInfo(None, name, 0)
            return True
        except Exception as exc:
            if getattr(exc, "winerror", None) != 2221:
                raise TransactionFailure(EXIT_ACCOUNT_FAILED, "Broker sandbox account could not be verified") from exc
    try:
        win32net.NetLocalGroupGetInfo(None, ACCOUNT_GROUP, 0)
        return True
    except Exception as exc:
        if getattr(exc, "winerror", None) != 2220:
            raise TransactionFailure(EXIT_ACCOUNT_FAILED, "Broker sandbox group could not be verified") from exc
    return False


def secrets_token() -> str:
    return secrets.token_urlsafe(32)
