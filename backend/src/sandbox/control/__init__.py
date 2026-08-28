"""Sandbox approval, execution decision, and Broker client control plane."""

from importlib import import_module
from typing import Any

_EXPORTS = {
    "ApprovalDecision": ("approvals", "ApprovalDecision"),
    "ApprovalGrant": ("approvals", "ApprovalGrant"),
    "ApprovalStore": ("approvals", "ApprovalStore"),
    "authorization_hash": ("approvals", "authorization_hash"),
    "BrokerManagedProcess": ("broker", "BrokerManagedProcess"),
    "BrokerStatus": ("broker", "BrokerStatus"),
    "WindowsBrokerClient": ("broker", "WindowsBrokerClient"),
    "SandboxExecutionDecision": ("decision", "SandboxExecutionDecision"),
}


def __getattr__(name: str) -> Any:
    try:
        module_name, attribute = _EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(name) from exc
    value = getattr(import_module(f"{__name__}.{module_name}"), attribute)
    globals()[name] = value
    return value


__all__ = list(_EXPORTS)
