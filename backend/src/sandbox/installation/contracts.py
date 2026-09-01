"""Validated input and stable exit codes for the elevated installer."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

EXIT_OK = 0
EXIT_INVALID = 2
EXIT_SERVICE_FAILED = 3
EXIT_ACL_FAILED = 4
EXIT_SERVICE_START_FAILED = 5
EXIT_FILESYSTEM_FAILED = 6
EXIT_SERVICE_STOP_FAILED = 7
EXIT_ACCOUNT_FAILED = 8
EXIT_CREDENTIAL_FAILED = 9
EXIT_RIGHTS_FAILED = 10
EXIT_NETWORK_FAILED = 11

BROKER_SERVICE_CLASS = "sandbox_service_bootstrap.MiniAgentSandboxBrokerService"


class TransactionFailure(RuntimeError):
    """Installer failure with the process exit code expected by the caller."""

    def __init__(self, exit_code: int, message: str) -> None:
        super().__init__(message)
        self.exit_code = exit_code


def validate_payload(
    payload: Mapping[str, Any],
) -> tuple[
    str,
    str,
    tuple[str, ...],
    str | None,
    str | None,
    Path | None,
    Path | None,
    Path | None,
    Path | None,
    tuple[Path, ...],
    int,
]:
    """Validate untrusted command-line JSON before any privileged mutation."""

    operation = payload.get("operation")
    service_name = payload.get("service_name")
    service_command = payload.get("service_command")
    service_class = payload.get("service_class")
    backend_sid = payload.get("backend_sid")
    backend_sid_path = payload.get("backend_sid_path")
    program_data_path = payload.get("program_data_path")
    service_code_path = payload.get("service_code_path")
    service_code_boundary_path = payload.get("service_code_boundary_path")
    service_runtime_paths = payload.get("service_runtime_paths", [])
    proxy_port = payload.get("proxy_port", 17831)
    if operation not in {"install", "repair", "reinstall"} or not isinstance(service_name, str) or not service_name:
        raise ValueError("invalid Broker installation operation")
    if (
        not isinstance(service_command, list)
        or not service_command
        or any(not isinstance(item, str) or not item for item in service_command)
    ):
        raise ValueError("invalid Broker service command")
    if service_class is not None and (
        not isinstance(service_class, str) or not service_class or "\r" in service_class or "\n" in service_class
    ):
        raise ValueError("invalid Broker service class")
    if backend_sid is not None and (not isinstance(backend_sid, str) or not backend_sid):
        raise ValueError("invalid Broker backend SID")
    if backend_sid_path is not None and not isinstance(backend_sid_path, str):
        raise ValueError("invalid Broker SID path")
    if program_data_path is not None and not isinstance(program_data_path, str):
        raise ValueError("invalid Broker ProgramData path")
    if service_code_path is not None and not isinstance(service_code_path, str):
        raise ValueError("invalid Broker source path")
    if service_code_boundary_path is not None and not isinstance(service_code_boundary_path, str):
        raise ValueError("invalid Broker source boundary path")
    if not isinstance(service_runtime_paths, list) or any(
        not isinstance(item, str) or not item for item in service_runtime_paths
    ):
        raise ValueError("invalid Broker runtime paths")
    sid_path = Path(backend_sid_path) if backend_sid_path else None
    data_path = Path(program_data_path) if program_data_path else None
    code_path = Path(service_code_path) if service_code_path else None
    code_boundary_path = Path(service_code_boundary_path) if service_code_boundary_path else None
    runtime_paths = tuple(Path(item) for item in service_runtime_paths)
    if sid_path is not None and not sid_path.is_absolute():
        raise ValueError("Broker SID path must be absolute")
    if data_path is not None and not data_path.is_absolute():
        raise ValueError("Broker ProgramData path must be absolute")
    if data_path is not None and (
        data_path.name.casefold() != "sandboxbroker" or data_path.parent.name.casefold() != "mini-agent"
    ):
        raise ValueError("Broker ProgramData path is outside the managed directory")
    if code_path is not None and not code_path.is_absolute():
        raise ValueError("Broker source path must be absolute")
    if code_boundary_path is not None and not code_boundary_path.is_absolute():
        raise ValueError("Broker source boundary path must be absolute")
    if (code_path is None) != (code_boundary_path is None):
        raise ValueError("Broker source path and boundary must be provided together")
    expected_service_class = BROKER_SERVICE_CLASS if code_path is None else rf"{code_path}\{BROKER_SERVICE_CLASS}"
    if service_class is not None and os.path.normcase(service_class) != os.path.normcase(expected_service_class):
        raise ValueError("Broker service class is outside the declared source path")
    if any(not path.is_absolute() or len(path.parts) < 3 for path in runtime_paths):
        raise ValueError("Broker runtime path must be a safe absolute path")
    if isinstance(proxy_port, bool) or not isinstance(proxy_port, int) or not 1 <= proxy_port <= 65535:
        raise ValueError("Broker proxy port is invalid")
    return (
        operation,
        service_name,
        tuple(service_command),
        service_class,
        backend_sid,
        sid_path,
        data_path,
        code_path,
        code_boundary_path,
        runtime_paths,
        proxy_port,
    )
