"""Per-reservation private desktop used by restricted child processes."""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass
from typing import Any

from ..errors import SandboxInitializationError
from .api import _modules

_WINDOW_STATION_LOCK = threading.RLock()


@dataclass(slots=True)
class WindowsPrivateDesktop:
    station_handle: Any
    station_name: str
    logon_sid: str
    station_ace_added: bool
    station_rights: int
    handle: Any
    name: str

    @property
    def startup_name(self) -> str:
        return rf"{self.station_name}\{self.name}"

    @classmethod
    def create(cls, logon_sid: str, service_sid: str) -> WindowsPrivateDesktop:
        modules = _modules()
        service = modules["service"]
        con = modules["con"]
        desktop_all = (
            con.DESKTOP_READOBJECTS
            | con.DESKTOP_CREATEWINDOW
            | con.DESKTOP_CREATEMENU
            | con.DESKTOP_HOOKCONTROL
            | con.DESKTOP_JOURNALRECORD
            | con.DESKTOP_JOURNALPLAYBACK
            | con.DESKTOP_ENUMERATE
            | con.DESKTOP_WRITEOBJECTS
            | con.DESKTOP_SWITCHDESKTOP
            | con.DELETE
            | con.READ_CONTROL
            | con.WRITE_DAC
            | con.WRITE_OWNER
        )
        participant = desktop_all & ~(con.DELETE | con.WRITE_DAC | con.WRITE_OWNER)
        attributes = _security_attributes(desktop_all, participant, logon_sid, service_sid)
        name = f"MiniAgentSandboxDesktop-{uuid.uuid4().hex}"

        with _WINDOW_STATION_LOCK:
            current = service.GetProcessWindowStation()
            station_participant = _station_participant_rights(con)
            station_ace_added = False
            try:
                station_ace_added = _grant_station_access(current, logon_sid, station_participant)
                station_name = str(service.GetUserObjectInformation(current, 2))
                if not station_name:
                    raise OSError("window station name is unavailable")
                handle = service.CreateDesktop(name, 0, desktop_all, attributes)
            except Exception as exc:  # pragma: no cover - requires service desktop
                if station_ace_added:
                    _revoke_station_access(current, logon_sid, station_participant)
                raise SandboxInitializationError("Broker private desktop creation failed") from exc
        return cls(current, station_name, logon_sid, station_ace_added, station_participant, handle, name)

    def close(self) -> None:
        handle, self.handle = self.handle, None
        if handle is None:
            return
        try:
            handle.CloseDesktop()
        except Exception:
            pass
        station, self.station_handle = self.station_handle, None
        if station is not None and self.station_ace_added:
            with _WINDOW_STATION_LOCK:
                _revoke_station_access(station, self.logon_sid, self.station_rights)


def _security_attributes(full: int, participant: int, logon_sid: str, service_sid: str) -> Any:
    modules = _modules()
    security = modules["security"]
    attributes = modules["types"].SECURITY_ATTRIBUTES()
    descriptor = modules["types"].SECURITY_DESCRIPTOR()
    acl = security.ACL()
    acl.AddAccessAllowedAce(
        security.ACL_REVISION,
        full,
        security.CreateWellKnownSid(security.WinLocalSystemSid, None),
    )
    acl.AddAccessAllowedAce(
        security.ACL_REVISION,
        full,
        security.ConvertStringSidToSid(service_sid),
    )
    acl.AddAccessAllowedAce(
        security.ACL_REVISION,
        participant,
        security.ConvertStringSidToSid(logon_sid),
    )
    descriptor.SetSecurityDescriptorDacl(1, acl, 0)
    attributes.SECURITY_DESCRIPTOR = descriptor
    attributes.bInheritHandle = False
    return attributes


def _station_participant_rights(constants: Any) -> int:
    return int(
        constants.WINSTA_ACCESSCLIPBOARD
        | constants.WINSTA_ACCESSGLOBALATOMS
        | constants.WINSTA_CREATEDESKTOP
        | constants.WINSTA_ENUMDESKTOPS
        | constants.WINSTA_ENUMERATE
        | constants.WINSTA_EXITWINDOWS
        | constants.WINSTA_READATTRIBUTES
        | constants.WINSTA_READSCREEN
        | constants.WINSTA_WRITEATTRIBUTES
        | constants.READ_CONTROL
    )


def _grant_station_access(handle: Any, sid_value: str, rights: int) -> bool:
    modules = _modules()
    security = modules["security"]
    descriptor = security.GetUserObjectSecurity(handle, security.DACL_SECURITY_INFORMATION)
    dacl = descriptor.GetSecurityDescriptorDacl() or security.ACL()
    sid = security.ConvertStringSidToSid(sid_value)
    if any(
        (ace := dacl.GetAce(index))[0][0] == security.ACCESS_ALLOWED_ACE_TYPE
        and ace[2] == sid
        and int(ace[1]) & rights == rights
        for index in range(dacl.GetAceCount())
    ):
        return False
    dacl.AddAccessAllowedAce(security.ACL_REVISION, rights, sid)
    descriptor.SetSecurityDescriptorDacl(1, dacl, 0)
    security.SetUserObjectSecurity(handle, security.DACL_SECURITY_INFORMATION, descriptor)
    return True


def _revoke_station_access(handle: Any, sid_value: str, rights: int) -> None:
    modules = _modules()
    security = modules["security"]
    try:
        descriptor = security.GetUserObjectSecurity(handle, security.DACL_SECURITY_INFORMATION)
        dacl = descriptor.GetSecurityDescriptorDacl()
        if dacl is None:
            return
        sid = security.ConvertStringSidToSid(sid_value)
        matches = [
            index
            for index in range(dacl.GetAceCount())
            if (ace := dacl.GetAce(index))[0][0] == security.ACCESS_ALLOWED_ACE_TYPE
            and ace[2] == sid
            and int(ace[1]) == rights
        ]
        for index in reversed(matches):
            dacl.DeleteAce(index)
        if matches:
            descriptor.SetSecurityDescriptorDacl(1, dacl, 0)
            security.SetUserObjectSecurity(handle, security.DACL_SECURITY_INFORMATION, descriptor)
    except Exception:
        pass


__all__ = ["WindowsPrivateDesktop"]
