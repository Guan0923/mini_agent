"""Stable errors exposed by the Windows sandbox boundary."""

from __future__ import annotations

from enum import StrEnum


class SandboxFailureCode(StrEnum):
    INIT_FAILED = "init_failed"
    POLICY_FAILED = "policy_failed"
    RESOURCE_EXCEEDED = "resource_exceeded"
    ADMISSION_TIMEOUT = "admission_timeout"
    CLEANUP_PENDING = "cleanup_pending"


class SandboxError(RuntimeError):
    """Base error which never contains command lines or environment values."""

    def __init__(self, message: str, code: SandboxFailureCode) -> None:
        super().__init__(message)
        self.code = code


class SandboxPolicyError(SandboxError):
    def __init__(self, message: str) -> None:
        super().__init__(message, SandboxFailureCode.POLICY_FAILED)


class SandboxInitializationError(SandboxError):
    def __init__(self, message: str) -> None:
        super().__init__(message, SandboxFailureCode.INIT_FAILED)


class SandboxResourceExceeded(SandboxError):
    def __init__(self, message: str) -> None:
        super().__init__(message, SandboxFailureCode.RESOURCE_EXCEEDED)


class SandboxCleanupPending(SandboxError):
    def __init__(self, message: str) -> None:
        super().__init__(message, SandboxFailureCode.CLEANUP_PENDING)


__all__ = [
    "SandboxCleanupPending",
    "SandboxError",
    "SandboxFailureCode",
    "SandboxInitializationError",
    "SandboxPolicyError",
    "SandboxResourceExceeded",
]
