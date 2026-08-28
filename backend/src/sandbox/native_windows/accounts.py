"""Sandbox local-account provisioning and restricted tokens."""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import Any

from ..errors import SandboxInitializationError
from .api import _modules


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
