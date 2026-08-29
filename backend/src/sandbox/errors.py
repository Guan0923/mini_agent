"""Stable errors exposed by the Windows sandbox boundary."""

from __future__ import annotations

from enum import StrEnum


class SandboxFailureCode(StrEnum):
    INIT_FAILED = "init_failed"
    POLICY_FAILED = "policy_failed"
    RESOURCE_EXCEEDED = "resource_exceeded"
    ADMISSION_TIMEOUT = "admission_timeout"
    CLEANUP_PENDING = "cleanup_pending"


class BrokerInstallFailureCode(StrEnum):
    """Stable, user-safe failure categories for Broker control-plane actions."""

    UAC_CANCELLED = "broker_uac_cancelled"
    ADMIN_REQUIRED = "broker_admin_required"
    DEPENDENCY_MISSING = "broker_dependency_missing"
    ACCOUNT_FAILED = "broker_account_failed"
    CREDENTIAL_FAILED = "broker_credential_failed"
    PRIVILEGE_FAILED = "broker_privilege_failed"
    NETWORK_FAILED = "broker_network_failed"
    ACL_FAILED = "broker_acl_failed"
    SERVICE_FAILED = "broker_service_failed"
    SERVICE_STOP_FAILED = "broker_service_stop_failed"
    SERVICE_START_FAILED = "broker_service_start_failed"
    NOT_READY = "broker_not_ready"
    UNKNOWN = "broker_install_failed"


class BrokerStatusFailureCode(StrEnum):
    """Stable failure categories returned by the Broker health endpoint."""

    UNAVAILABLE = "broker_unavailable"
    NOT_INSTALLED = "broker_not_installed"
    SERVICE_CONFIGURATION_INVALID = "broker_service_configuration_invalid"
    READY_MARKER_UNAVAILABLE = "broker_ready_marker_unavailable"
    READY_MARKER_INVALID = "broker_ready_marker_invalid"
    PROXY_CONFIGURATION_INVALID = "broker_proxy_configuration_invalid"
    INSTALLATION_KEY_MISSING = "broker_installation_key_missing"
    PIPE_UNAVAILABLE = "broker_pipe_unavailable"
    PROTOCOL_INCOMPATIBLE = "broker_protocol_incompatible"
    TOKEN_MODEL_INCOMPATIBLE = "broker_token_model_incompatible"
    GENERATION_MISMATCH = "broker_generation_mismatch"
    RESPONSE_INVALID = "broker_response_invalid"
    RESPONSE_AUTHENTICATION_FAILED = "broker_response_authentication_failed"
    UNHEALTHY = "broker_unhealthy"
    STATUS_FAILED = "broker_status_failed"


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


class BrokerInstallationError(SandboxInitializationError):
    """A categorized Broker installation failure safe to expose at the API boundary."""

    def __init__(self, code: BrokerInstallFailureCode, message: str) -> None:
        super().__init__(message)
        self.broker_code = code
        self.safe_message = message


class SandboxResourceExceeded(SandboxError):
    def __init__(self, message: str) -> None:
        super().__init__(message, SandboxFailureCode.RESOURCE_EXCEEDED)


class SandboxCleanupPending(SandboxError):
    def __init__(self, message: str) -> None:
        super().__init__(message, SandboxFailureCode.CLEANUP_PENDING)


__all__ = [
    "SandboxCleanupPending",
    "BrokerInstallFailureCode",
    "BrokerInstallationError",
    "BrokerStatusFailureCode",
    "SandboxError",
    "SandboxFailureCode",
    "SandboxInitializationError",
    "SandboxPolicyError",
    "SandboxResourceExceeded",
]
