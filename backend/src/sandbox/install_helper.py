"""Privileged, single-transaction Windows Broker installer.

This module is launched once through UAC by :class:`WindowsServiceInstaller`.
It intentionally contains no network or application state and returns only a
small exit code to the unprivileged parent process.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import struct
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

EXIT_OK = 0
EXIT_INVALID = 2
EXIT_SERVICE_FAILED = 3
EXIT_ACL_FAILED = 4
EXIT_SERVICE_START_FAILED = 5
EXIT_FILESYSTEM_FAILED = 6
EXIT_SERVICE_STOP_FAILED = 7

_SERVICE_STOPPED = 1
_SERVICE_STOP_TIMEOUT_SECONDS = 5.0
_SERVICE_STOP_POLL_SECONDS = 0.1


class _TransactionFailure(RuntimeError):
    def __init__(self, exit_code: int, message: str) -> None:
        super().__init__(message)
        self.exit_code = exit_code


def _powershell_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _icacls_sid(value: str) -> str:
    """Format a numeric SID for icacls (numeric SIDs require a leading *)."""

    sid = value.strip()
    if not re.fullmatch(r"S-\d+(?:-\d+)+", sid, flags=re.IGNORECASE):
        raise ValueError("Broker backend SID is invalid")
    return f"*{sid}"


def _service_sid(service_name: str) -> str:
    """Return the deterministic Windows virtual-service SID for a service name."""

    digest = hashlib.sha1(service_name.upper().encode("utf-16le")).digest()
    authorities = struct.unpack("<5I", digest)
    return "S-1-5-80-" + "-".join(str(authority) for authority in authorities)


def _validate_payload(
    payload: Mapping[str, Any],
) -> tuple[str, str, tuple[str, ...], str | None, Path | None, Path | None, Path | None, Path | None]:
    operation = payload.get("operation")
    service_name = payload.get("service_name")
    service_command = payload.get("service_command")
    backend_sid = payload.get("backend_sid")
    backend_sid_path = payload.get("backend_sid_path")
    program_data_path = payload.get("program_data_path")
    service_code_path = payload.get("service_code_path")
    service_code_boundary_path = payload.get("service_code_boundary_path")
    if operation not in {"install", "repair"} or not isinstance(service_name, str) or not service_name:
        raise ValueError("invalid Broker installation operation")
    if (
        not isinstance(service_command, list)
        or not service_command
        or any(not isinstance(item, str) or not item for item in service_command)
    ):
        raise ValueError("invalid Broker service command")
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
    sid_path = Path(backend_sid_path) if backend_sid_path else None
    data_path = Path(program_data_path) if program_data_path else None
    code_path = Path(service_code_path) if service_code_path else None
    code_boundary_path = Path(service_code_boundary_path) if service_code_boundary_path else None
    if sid_path is not None and not sid_path.is_absolute():
        raise ValueError("Broker SID path must be absolute")
    if data_path is not None and not data_path.is_absolute():
        raise ValueError("Broker ProgramData path must be absolute")
    if code_path is not None and not code_path.is_absolute():
        raise ValueError("Broker source path must be absolute")
    if code_boundary_path is not None and not code_boundary_path.is_absolute():
        raise ValueError("Broker source boundary path must be absolute")
    if (code_path is None) != (code_boundary_path is None):
        raise ValueError("Broker source path and boundary must be provided together")
    return (
        operation,
        service_name,
        tuple(service_command),
        backend_sid,
        sid_path,
        data_path,
        code_path,
        code_boundary_path,
    )


def _run(
    command: Sequence[str],
    *,
    failure_code: int = EXIT_SERVICE_FAILED,
    accepted_returncodes: frozenset[int] = frozenset({0}),
) -> None:
    try:
        result = subprocess.run(command, check=False, capture_output=True)
    except OSError as exc:
        raise _TransactionFailure(EXIT_FILESYSTEM_FAILED, "Broker service command could not start") from exc
    if result.returncode not in accepted_returncodes:
        raise _TransactionFailure(failure_code, "Broker service command failed")


def _query_service_state(service_name: str) -> int:
    """Read the numeric SCM state without parsing localized command output."""

    manager = None
    service = None
    win32service = None
    try:
        import win32service as win32service_module  # type: ignore[import-not-found]

        win32service = win32service_module
        manager = win32service.OpenSCManager(None, None, win32service.SC_MANAGER_CONNECT)
        service = win32service.OpenService(manager, service_name, win32service.SERVICE_QUERY_STATUS)
        status = win32service.QueryServiceStatusEx(service)
        return int(status["CurrentState"])
    except Exception as exc:
        raise OSError("Broker service state is unavailable") from exc
    finally:
        if win32service is not None and service is not None:
            try:
                win32service.CloseServiceHandle(service)
            except Exception:
                pass
        if win32service is not None and manager is not None:
            try:
                win32service.CloseServiceHandle(manager)
            except Exception:
                pass


def _wait_for_service_stopped(
    service_name: str,
    *,
    state_reader: Callable[[str], int] | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], Any] = time.sleep,
) -> bool:
    reader = state_reader or _query_service_state
    deadline = clock() + _SERVICE_STOP_TIMEOUT_SECONDS
    while True:
        try:
            state = int(reader(service_name))
        except Exception:
            return False
        if state == _SERVICE_STOPPED:
            return True
        remaining = deadline - clock()
        if remaining <= 0:
            return False
        sleeper(min(_SERVICE_STOP_POLL_SECONDS, remaining))


def _stop_service_for_repair(
    service_name: str,
    *,
    runner: Callable[..., Any] | None = None,
    state_reader: Callable[[str], int] | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], Any] = time.sleep,
) -> None:
    command_runner = runner or subprocess.run
    try:
        command_runner(["sc.exe", "stop", service_name], check=False, capture_output=True)
    except OSError:
        # SCM state is authoritative: a command failure may race with the
        # service reaching STOPPED, so always perform the bounded state check.
        pass
    if _wait_for_service_stopped(
        service_name,
        state_reader=state_reader,
        clock=clock,
        sleeper=sleeper,
    ):
        return
    raise _TransactionFailure(EXIT_SERVICE_STOP_FAILED, "Broker service did not stop")


def _persist_sid(path: Path | None, value: str | None) -> None:
    if path is None or value is None:
        return
    _icacls_sid(value)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Replace the file instead of writing in place.  Older installations
        # may have an empty/protected DACL on backend.sid, which can deny an
        # administrator an in-place write.  The parent directory is already
        # controlled by the elevated transaction and permits replacement.
        fd, temporary = tempfile.mkstemp(prefix=".backend.sid.", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="ascii") as stream:
                stream.write(value)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
    except OSError as exc:
        raise OSError("Broker backend SID could not be persisted") from exc


def _sid_acl_command(
    path: Path,
    backend_sid: str,
    service_name: str | None,
) -> list[str]:
    grants = [
        "SYSTEM:(F)",
        "Administrators:(F)",
        f"{_icacls_sid(backend_sid)}:(M)",
    ]
    if service_name is not None:
        grants.append(f"{_icacls_sid(_service_sid(service_name))}:(R)")
    return [
        "icacls.exe",
        str(path),
        "/inheritance:r",
        "/grant:r",
        *grants,
        "/C",
    ]


def _program_data_acl_commands(
    path: Path,
    sid_path: Path,
    backend_sid: str,
    service_name: str,
) -> list[list[str]]:
    if not backend_sid or not path.is_absolute() or len(path.parts) < 3:
        raise ValueError("Broker ProgramData path is invalid")
    return [
        [
            "icacls.exe",
            str(path),
            "/inheritance:r",
            "/grant:r",
            "SYSTEM:(OI)(CI)(F)",
            "Administrators:(OI)(CI)(F)",
            f"{_icacls_sid(backend_sid)}:(OI)(CI)(M)",
            f"{_icacls_sid(_service_sid(service_name))}:(OI)(CI)(M)",
            "/T",
            "/C",
        ],
        _sid_acl_command(sid_path, backend_sid, service_name),
    ]


def _managed_file_acl_commands(path: Path, backend_sid: str, service_name: str) -> list[list[str]]:
    return [
        ["takeown.exe", "/F", str(path), "/A"],
        [
            "icacls.exe",
            str(path),
            "/inheritance:r",
            "/grant:r",
            "SYSTEM:(F)",
            "Administrators:(F)",
            f"{_icacls_sid(backend_sid)}:(R)",
            f"{_icacls_sid(_service_sid(service_name))}:(M)",
            "/C",
        ],
    ]


def _directory_contains(path: Path, name: str) -> bool:
    try:
        with os.scandir(path) as entries:
            return any(entry.name.casefold() == name.casefold() for entry in entries)
    except OSError as exc:
        raise OSError("Broker ProgramData directory is unavailable") from exc


def _secure_program_data(path: Path | None, sid_path: Path | None, service_name: str) -> None:
    if path is None or sid_path is None:
        return
    try:
        backend_sid = sid_path.read_text(encoding="ascii").strip()
    except OSError as exc:
        raise OSError("Broker backend SID is unavailable") from exc
    for command in _program_data_acl_commands(path, sid_path, backend_sid, service_name):
        _run(command, failure_code=EXIT_ACL_FAILED)
    for name in ("installation.id", "installation.key.dpapi"):
        managed_path = path / name
        if _directory_contains(path, name):
            for command in _managed_file_acl_commands(managed_path, backend_sid, service_name):
                _run(command, failure_code=EXIT_ACL_FAILED)


@dataclass(frozen=True, slots=True)
class _SourceAclGrant:
    path: Path
    sid: str
    rights: str
    inherit: bool

    def runner_command(self) -> list[str]:
        return ["win32-acl", str(self.path), self.sid, self.rights, "inherit" if self.inherit else "direct"]


def _source_acl_grants(path: Path, boundary: Path, service_name: str) -> list[_SourceAclGrant]:
    if not path.is_absolute() or not boundary.is_absolute() or len(path.parts) < 3:
        raise ValueError("Broker source path is invalid")
    try:
        path.relative_to(boundary)
    except ValueError as exc:
        raise ValueError("Broker source path is outside its boundary") from exc
    if path == boundary:
        raise ValueError("Broker source path must be below its boundary")
    service_sid = _service_sid(service_name)
    return [_SourceAclGrant(path, service_sid, "RX", True)]


def _apply_source_acl_grant(grant: _SourceAclGrant) -> None:
    try:
        import ntsecuritycon  # type: ignore[import-not-found]
        import win32security  # type: ignore[import-not-found]

        sid = win32security.ConvertStringSidToSid(grant.sid)
        descriptor = win32security.GetNamedSecurityInfo(
            str(grant.path),
            win32security.SE_FILE_OBJECT,
            win32security.DACL_SECURITY_INFORMATION,
        )
        dacl = descriptor.GetSecurityDescriptorDacl() or win32security.ACL()
        inheritance = win32security.CONTAINER_INHERIT_ACE | win32security.OBJECT_INHERIT_ACE if grant.inherit else 0
        access = (
            ntsecuritycon.FILE_GENERIC_READ | ntsecuritycon.FILE_GENERIC_EXECUTE
            if grant.rights == "RX"
            else ntsecuritycon.FILE_TRAVERSE
        )
        for index in range(dacl.GetAceCount()):
            ace = dacl.GetAce(index)
            if (
                ace[0][0] == win32security.ACCESS_ALLOWED_ACE_TYPE
                and ace[2] == sid
                and ace[1] & access == access
                and ace[0][1] & inheritance == inheritance
            ):
                return
        dacl.AddAccessAllowedAceEx(win32security.ACL_REVISION_DS, inheritance, access, sid)
        win32security.SetNamedSecurityInfo(
            str(grant.path),
            win32security.SE_FILE_OBJECT,
            win32security.DACL_SECURITY_INFORMATION,
            None,
            None,
            dacl,
            None,
        )
    except Exception as exc:
        raise OSError("Broker source ACL could not be configured") from exc


def _secure_source_code(path: Path | None, boundary: Path | None, service_name: str) -> None:
    if path is None or boundary is None:
        return
    for grant in _source_acl_grants(path, boundary, service_name):
        _apply_source_acl_grant(grant)


def run_transaction(payload: Mapping[str, Any]) -> int:
    """Run one fully elevated Broker installation transaction."""

    (
        operation,
        service_name,
        service_command,
        backend_sid,
        sid_path,
        data_path,
        code_path,
        code_boundary_path,
    ) = _validate_payload(payload)
    _persist_sid(sid_path, backend_sid)
    if sid_path is not None and backend_sid is not None:
        # Restore a usable descriptor before touching SCM.  The service ACE is
        # added after the service exists and its virtual account is resolvable.
        _run(_sid_acl_command(sid_path, backend_sid, None), failure_code=EXIT_ACL_FAILED)
    command = subprocess.list2cmdline(list(service_command))
    if operation == "install":
        _run(
            [
                "sc.exe",
                "create",
                service_name,
                "type=",
                "own",
                "start=",
                "demand",
                "obj=",
                f"NT SERVICE\\{service_name}",
                "binPath=",
                command,
            ]
        )
        _run(["sc.exe", "sidtype", service_name, "unrestricted"])
    else:
        _stop_service_for_repair(service_name)
        _run(
            [
                "sc.exe",
                "config",
                service_name,
                "type=",
                "own",
                "start=",
                "demand",
                "obj=",
                f"NT SERVICE\\{service_name}",
                "binPath=",
                command,
            ]
        )
        _run(["sc.exe", "sidtype", service_name, "unrestricted"])
    _secure_program_data(data_path, sid_path, service_name)
    _secure_source_code(code_path, code_boundary_path, service_name)
    _run(
        ["sc.exe", "start", service_name],
        failure_code=EXIT_SERVICE_START_FAILED,
        accepted_returncodes=frozenset({0, 1056}),
    )
    return EXIT_OK


def _decode_payload(value: str) -> dict[str, Any]:
    raw = base64.urlsafe_b64decode(value.encode("ascii"))
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Broker installation payload must be an object")
    return payload


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        return EXIT_INVALID
    try:
        return run_transaction(_decode_payload(args[0]))
    except (ValueError, TypeError, json.JSONDecodeError, UnicodeError):
        return EXIT_INVALID
    except OSError:
        return EXIT_FILESYSTEM_FAILED
    except _TransactionFailure as exc:
        return exc.exit_code


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "EXIT_ACL_FAILED",
    "EXIT_FILESYSTEM_FAILED",
    "EXIT_INVALID",
    "EXIT_OK",
    "EXIT_SERVICE_FAILED",
    "EXIT_SERVICE_START_FAILED",
    "EXIT_SERVICE_STOP_FAILED",
    "main",
    "run_transaction",
]
