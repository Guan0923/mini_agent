"""Lazy pywin32 primitives used by the Windows Broker service."""

from .accounts import (
    WindowsReservedToken,
    WindowsRestrictedTokenFactory,
    WindowsSandboxAccount,
    random_capability_sid,
)
from .desktop import WindowsPrivateDesktop
from .jobs import WindowsJobObject
from .security import WindowsAclManager, windows_pipe_security_attributes, windows_service_sid

__all__ = [
    "WindowsAclManager",
    "WindowsJobObject",
    "WindowsPrivateDesktop",
    "WindowsRestrictedTokenFactory",
    "WindowsReservedToken",
    "WindowsSandboxAccount",
    "random_capability_sid",
    "windows_pipe_security_attributes",
    "windows_service_sid",
]
