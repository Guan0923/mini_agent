"""Sandbox process admission, launch, monitoring, manifest, and recovery."""

from importlib import import_module
from typing import Any

_EXPORTS = {
    "AggregateLimits": ("admission", "AggregateLimits"),
    "ResourceRequest": ("admission", "ResourceRequest"),
    "SandboxAdmission": ("admission", "SandboxAdmission"),
    "SandboxAdmissionTimeout": ("admission", "SandboxAdmissionTimeout"),
    "SandboxLauncher": ("launcher", "SandboxLauncher"),
    "ResourceManifest": ("manifest", "ResourceManifest"),
    "ResourceRecord": ("manifest", "ResourceRecord"),
    "SandboxResourceReclaimer": ("reclaimer", "SandboxResourceReclaimer"),
    "NullResourceProvider": ("resources", "NullResourceProvider"),
    "ResourceMonitor": ("resources", "ResourceMonitor"),
    "ResourceUsage": ("resources", "ResourceUsage"),
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
