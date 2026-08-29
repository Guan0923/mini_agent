"""Atomic backend ownership records for command ACL leases."""

from __future__ import annotations

import json
import os
import threading
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

from ..errors import SandboxInitializationError
from ..policy import FileAccessMode, remove_temp_dir


@dataclass(frozen=True, slots=True)
class CommandLease:
    job_id: str
    reservation_id: str
    logon_sid: str
    account_sid: str
    service_sid: str
    workspace: str
    temp_dir: str
    file_mode: str
    workspace_cap_sid: str
    temp_cap_sid: str
    capability_digest: str
    deny_paths: tuple[str, ...]
    path_identities: dict[str, str]


class CommandLeaseStore:
    """Persist active ACE ownership so a restarted backend can undo it."""

    _process_lock = threading.RLock()

    def __init__(self, path: Path, acl_manager) -> None:
        self.path = Path(path)
        self.acl_manager = acl_manager

    def add(self, lease: CommandLease) -> None:
        with self._process_lock:
            values = self._read()
            if any(item.job_id == lease.job_id for item in values):
                raise SandboxInitializationError("sandbox lease already exists")
            self._write((*values, lease))

    def remove(self, job_id: str) -> None:
        with self._process_lock:
            self._write(tuple(item for item in self._read() if item.job_id != job_id))

    def release(self, lease: CommandLease) -> bool:
        """Revoke one persisted lease without breaking same-account peers."""

        with self._process_lock:
            values = self._read()
            if not any(item.job_id == lease.job_id for item in values):
                return True
            remaining = tuple(item for item in values if item.job_id != lease.job_id)
            workspace = Path(lease.workspace)
            temp_path = Path(lease.temp_dir)
            checks = [
                self.acl_manager.revoke_lease(workspace, lease.workspace_cap_sid),
                self.acl_manager.revoke_lease(workspace, lease.service_sid),
            ]
            for deny_path in lease.deny_paths:
                checks.extend(
                    (
                        self.acl_manager.revoke_lease(Path(deny_path), lease.workspace_cap_sid),
                        self.acl_manager.revoke_lease(Path(deny_path), lease.temp_cap_sid),
                    )
                )
            account_still_used = any(
                item.account_sid == lease.account_sid and Path(item.workspace) == workspace for item in remaining
            )
            if not account_still_used:
                checks.append(self.acl_manager.revoke_lease(workspace, lease.account_sid))
            if temp_path.exists():
                checks.extend(
                    (
                        self.acl_manager.revoke_lease(temp_path, lease.logon_sid),
                        self.acl_manager.revoke_lease(temp_path, lease.workspace_cap_sid),
                        self.acl_manager.revoke_lease(temp_path, lease.temp_cap_sid),
                        self.acl_manager.revoke_lease(temp_path, lease.account_sid),
                    )
                )
            checks.append(remove_temp_dir(temp_path))
            if all(checks):
                self._write(remaining)
                return True
            return False

    def recover(self) -> tuple[str, ...]:
        recovered: list[str] = []
        for lease in self._read():
            if self.release(lease):
                recovered.append(lease.job_id)
        return tuple(recovered)

    def _read(self) -> tuple[CommandLease, ...]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return ()
        except (OSError, ValueError) as exc:
            raise SandboxInitializationError("sandbox lease manifest is invalid") from exc
        if not isinstance(raw, list):
            raise SandboxInitializationError("sandbox lease manifest is invalid")
        result: list[CommandLease] = []
        try:
            for item in raw:
                if not isinstance(item, dict):
                    raise TypeError
                lease = CommandLease(**item)
                FileAccessMode(lease.file_mode)
                if (
                    not lease.job_id
                    or not lease.reservation_id
                    or not lease.logon_sid
                    or not lease.account_sid
                    or not lease.service_sid
                    or not lease.workspace_cap_sid
                    or not lease.temp_cap_sid
                    or not lease.capability_digest
                    or not isinstance(lease.deny_paths, (list, tuple))
                    or not all(isinstance(path, str) and path for path in lease.deny_paths)
                    or not isinstance(lease.path_identities, dict)
                    or not all(
                        isinstance(path, str) and path and isinstance(identity, str) and identity
                        for path, identity in lease.path_identities.items()
                    )
                ):
                    raise ValueError
                result.append(
                    CommandLease(
                        **{
                            **asdict(lease),
                            "deny_paths": tuple(lease.deny_paths),
                            "path_identities": dict(lease.path_identities),
                        }
                    )
                )
        except (TypeError, ValueError) as exc:
            raise SandboxInitializationError("sandbox lease manifest is invalid") from exc
        return tuple(result)

    def _write(self, values: tuple[CommandLease, ...]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}")
        try:
            with temporary.open("w", encoding="utf-8", newline="\n") as stream:
                json.dump([asdict(item) for item in values], stream, sort_keys=True, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


__all__ = ["CommandLease", "CommandLeaseStore"]
