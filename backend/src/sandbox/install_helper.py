"""Privileged, single-transaction Windows Broker installer.

This module is launched once through UAC by :class:`WindowsServiceInstaller`.
It intentionally contains no network or application state and returns only a
small exit code to the unprivileged parent process.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from .installation.access_policy import (
    _apply_source_acl_grant,
    _directory_contains,
    _icacls_sid,
    _iter_acl_tree,
    _managed_file_acl_commands,
    _program_data_acl_commands,
    _runtime_acl_grants,
    _secure_source_code,
    _sensitive_file_acl_commands,
    _service_class_command,
    _service_sid,
    _sid_acl_command,
    _source_acl_grants,
)
from .installation.accounts import (
    provision_fixed_accounts as _provision_fixed_accounts_impl,
)
from .installation.accounts import (
    remove_owned_accounts as _remove_owned_accounts,
)
from .installation.contracts import (
    EXIT_ACCOUNT_FAILED,
    EXIT_ACL_FAILED,
    EXIT_CREDENTIAL_FAILED,
    EXIT_FILESYSTEM_FAILED,
    EXIT_INVALID,
    EXIT_NETWORK_FAILED,
    EXIT_OK,
    EXIT_RIGHTS_FAILED,
    EXIT_SERVICE_FAILED,
    EXIT_SERVICE_START_FAILED,
    EXIT_SERVICE_STOP_FAILED,
)
from .installation.contracts import (
    TransactionFailure as _TransactionFailure,
)
from .installation.contracts import (
    validate_payload as _validate_payload,
)

_SERVICE_STOPPED = 1
_SERVICE_STOP_TIMEOUT_SECONDS = 5.0
_SERVICE_STOP_POLL_SECONDS = 0.1


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


def _service_exists(service_name: str) -> bool:
    try:
        result = subprocess.run(["sc.exe", "query", service_name], check=False, capture_output=True)
    except OSError as exc:
        raise OSError("Broker service state is unavailable") from exc
    return int(result.returncode) == 0


def _wait_for_service_deleted(
    service_name: str,
    *,
    exists: Callable[[str], bool] | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], Any] = time.sleep,
) -> bool:
    checker = exists or _service_exists
    deadline = clock() + _SERVICE_STOP_TIMEOUT_SECONDS
    while True:
        try:
            present = bool(checker(service_name))
        except Exception:
            return False
        if not present:
            return True
        remaining = deadline - clock()
        if remaining <= 0:
            return False
        sleeper(min(_SERVICE_STOP_POLL_SECONDS, remaining))


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


def _secure_program_data(path: Path | None, sid_path: Path | None, service_name: str) -> None:
    if path is None or sid_path is None:
        return
    try:
        backend_sid = sid_path.read_text(encoding="ascii").strip()
    except OSError as exc:
        raise OSError("Broker backend SID is unavailable") from exc
    for command in _program_data_acl_commands(path, sid_path, backend_sid, service_name):
        _run(command, failure_code=EXIT_ACL_FAILED)
    for name in (
        "installation.id",
        "installation.key.dpapi",
        "ready.json",
        "control-plane.jsonl",
        "resources.json",
    ):
        managed_path = path / name
        if _directory_contains(path, name):
            for command in _managed_file_acl_commands(managed_path, backend_sid, service_name):
                _run(command, failure_code=EXIT_ACL_FAILED)
    credential_path = path / "accounts.dpapi"
    if _directory_contains(path, credential_path.name):
        for command in _sensitive_file_acl_commands(credential_path, service_name):
            _run(command, failure_code=EXIT_ACL_FAILED)


def _provision_fixed_accounts(data_path: Path, service_name: str, proxy_port: int):
    """Stable facade for tests and the elevated transaction orchestrator."""

    return _provision_fixed_accounts_impl(
        data_path,
        service_name,
        proxy_port,
        configure_network=_configure_static_network,
    )


def _remove_service_rights(service_name: str) -> None:
    try:
        import win32security  # type: ignore[import-not-found]

        policy_handle = win32security.LsaOpenPolicy(None, win32security.POLICY_ALL_ACCESS)
        service_sid = win32security.ConvertStringSidToSid(_service_sid(service_name))
        win32security.LsaRemoveAccountRights(policy_handle, service_sid, True, ())
    except Exception as exc:
        winerror = getattr(exc, "winerror", None)
        if winerror not in {2, 1332}:
            raise _TransactionFailure(EXIT_RIGHTS_FAILED, "Broker service rights could not be removed") from exc


def _remove_static_network() -> None:
    try:
        from .native_windows.wfp import remove_static_wfp

        remove_static_wfp()
    except Exception as exc:
        raise _TransactionFailure(EXIT_NETWORK_FAILED, "Broker network policy could not be removed") from exc


def secrets_token() -> str:
    import secrets

    return secrets.token_urlsafe(32)


def _configure_static_network(offline_sid: str, online_sid: str, proxy_port: int) -> None:
    from .native_windows.wfp import configure_static_wfp

    configure_static_wfp(offline_sid, online_sid, proxy_port)
    # Remove pre-v3 Defender Firewall rules only after the atomic WFP policy is
    # active.  This avoids both an enforcement gap and stale proxy-port rules.
    script = """
$ErrorActionPreference='Stop'
Get-NetFirewallRule -Name 'MiniAgentSandbox-*' -ErrorAction SilentlyContinue | Remove-NetFirewallRule
"""
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    _run(
        ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
        failure_code=EXIT_NETWORK_FAILED,
    )


def _remove_source_acl_grant(path: Path, sid_text: str) -> None:
    if not path.exists():
        return
    try:
        import win32security  # type: ignore[import-not-found]

        sid = win32security.ConvertStringSidToSid(sid_text)
        descriptor = win32security.GetNamedSecurityInfo(
            str(path),
            win32security.SE_FILE_OBJECT,
            win32security.DACL_SECURITY_INFORMATION,
        )
        dacl = descriptor.GetSecurityDescriptorDacl()
        if dacl is None:
            return
        matching = [index for index in range(dacl.GetAceCount()) if dacl.GetAce(index)[2] == sid]
        for index in reversed(matching):
            dacl.DeleteAce(index)
        if matching:
            win32security.SetNamedSecurityInfo(
                str(path),
                win32security.SE_FILE_OBJECT,
                win32security.DACL_SECURITY_INFORMATION,
                None,
                None,
                dacl,
                None,
            )
    except Exception as exc:
        raise _TransactionFailure(EXIT_ACL_FAILED, "Broker source ACL could not be removed") from exc


def _remove_service_acls(
    code_path: Path | None,
    code_boundary_path: Path | None,
    runtime_paths: Sequence[Path],
    service_executable: Path,
    service_name: str,
) -> None:
    service_sid = _service_sid(service_name)
    grants = []
    if code_path is not None and code_boundary_path is not None:
        grants.extend(_source_acl_grants(code_path, code_boundary_path, service_name))
    grants.extend(_runtime_acl_grants(runtime_paths, service_executable, service_name))

    targets: set[Path] = set()
    for grant in grants:
        grant_targets = (
            (path for path, _is_directory in _iter_acl_tree(grant.path)) if grant.existing_children else (grant.path,)
        )
        targets.update(grant_targets)

    for path in sorted(targets, key=lambda item: (len(item.parts), os.path.normcase(str(item))), reverse=True):
        _remove_source_acl_grant(path, service_sid)


def _remove_program_data(path: Path) -> None:
    resolved = path.resolve(strict=False)
    if (
        resolved.name.casefold() != "sandboxbroker"
        or resolved.parent.name.casefold() != "mini-agent"
        or len(resolved.parts) < 3
    ):
        raise ValueError("Broker ProgramData path is outside the managed directory")
    try:
        shutil.rmtree(resolved)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise OSError("Broker ProgramData could not be removed") from exc


def _initialize_installation_key(data_path: Path) -> None:
    try:
        from .broker_service.credentials import DpapiKeyStore

        DpapiKeyStore(data_path / "installation.key.dpapi").ensure()
    except Exception as exc:
        raise _TransactionFailure(EXIT_CREDENTIAL_FAILED, "Broker installation key could not be created") from exc


def _uninstall_for_reinstall(
    service_name: str,
    service_command: Sequence[str],
    data_path: Path | None,
    code_path: Path | None,
    code_boundary_path: Path | None,
    runtime_paths: Sequence[Path],
) -> None:
    installed = _service_exists(service_name)
    if installed:
        _stop_service_for_repair(service_name)
    _remove_static_network()
    _remove_service_acls(
        code_path,
        code_boundary_path,
        runtime_paths,
        Path(service_command[0]),
        service_name,
    )
    _remove_service_rights(service_name)
    if data_path is not None:
        _remove_owned_accounts(data_path)
    if installed:
        _run(["sc.exe", "delete", service_name], failure_code=EXIT_SERVICE_FAILED)
        if not _wait_for_service_deleted(service_name):
            raise _TransactionFailure(EXIT_SERVICE_FAILED, "Broker service was not deleted")
    if data_path is not None:
        _remove_program_data(data_path)


def run_transaction(payload: Mapping[str, Any]) -> int:
    """Run one fully elevated Broker installation transaction."""

    (
        operation,
        service_name,
        service_command,
        service_class,
        backend_sid,
        sid_path,
        data_path,
        code_path,
        code_boundary_path,
        runtime_paths,
        proxy_port,
    ) = _validate_payload(payload)
    ready_path = data_path / "ready.json" if data_path is not None else None
    if ready_path is not None:
        try:
            ready_path.unlink()
        except FileNotFoundError:
            pass
    if operation == "reinstall":
        _uninstall_for_reinstall(
            service_name,
            service_command,
            data_path,
            code_path,
            code_boundary_path,
            runtime_paths,
        )
    _persist_sid(sid_path, backend_sid)
    if data_path is not None:
        _initialize_installation_key(data_path)
    if sid_path is not None and backend_sid is not None:
        # Restore a usable descriptor before touching SCM.  The service ACE is
        # added after the service exists and its virtual account is resolvable.
        _run(_sid_acl_command(sid_path, backend_sid, None), failure_code=EXIT_ACL_FAILED)
    command = subprocess.list2cmdline(list(service_command))
    if operation in {"install", "reinstall"}:
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
        if service_class is not None:
            _run(_service_class_command(service_name, service_class))
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
        if service_class is not None:
            _run(_service_class_command(service_name, service_class))
        _run(["sc.exe", "sidtype", service_name, "unrestricted"])
    ready_marker = None
    if data_path is not None:
        _, ready_marker = _provision_fixed_accounts(data_path, service_name, proxy_port)
    _secure_program_data(data_path, sid_path, service_name)
    _secure_source_code(code_path, code_boundary_path, service_name)
    for grant in _runtime_acl_grants(runtime_paths, Path(service_command[0]), service_name):
        _apply_source_acl_grant(grant)
    if ready_path is not None and ready_marker is not None:
        from .broker_service.readiness import write_ready_marker

        write_ready_marker(ready_path, ready_marker)
        # os.replace preserves the temporary file's descriptor.  Apply the
        # explicit non-sensitive marker ACL after the atomic replacement so a
        # stale or token-default DACL cannot lock the normal backend out.
        if backend_sid is None:
            raise _TransactionFailure(EXIT_ACL_FAILED, "Broker backend SID is unavailable")
        for command in _managed_file_acl_commands(ready_path, backend_sid, service_name):
            _run(command, failure_code=EXIT_ACL_FAILED)
    try:
        _run(
            ["sc.exe", "start", service_name],
            failure_code=EXIT_SERVICE_START_FAILED,
            accepted_returncodes=frozenset({0, 1056}),
        )
    except Exception:
        if ready_path is not None:
            try:
                ready_path.unlink()
            except FileNotFoundError:
                pass
        raise
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
    "EXIT_ACCOUNT_FAILED",
    "EXIT_ACL_FAILED",
    "EXIT_CREDENTIAL_FAILED",
    "EXIT_FILESYSTEM_FAILED",
    "EXIT_INVALID",
    "EXIT_OK",
    "EXIT_NETWORK_FAILED",
    "EXIT_RIGHTS_FAILED",
    "EXIT_SERVICE_FAILED",
    "EXIT_SERVICE_START_FAILED",
    "EXIT_SERVICE_STOP_FAILED",
    "main",
    "run_transaction",
    "_runtime_acl_grants",
]
