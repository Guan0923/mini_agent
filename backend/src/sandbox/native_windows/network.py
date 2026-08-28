"""Account-scoped outbound WFP firewall rules."""

from __future__ import annotations

import ipaddress
import os
import re
import subprocess
from base64 import b64encode
from collections.abc import Mapping, Sequence

from ..errors import SandboxInitializationError
from ..policy import NetworkMode


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
