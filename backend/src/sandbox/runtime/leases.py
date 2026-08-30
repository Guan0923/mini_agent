"""Atomic ownership records for ACL entries added by command jobs."""

from __future__ import annotations

import json
import os
import threading
import uuid
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from ..errors import SandboxInitializationError
from ..native_windows.security import AclLeaseEntry
from ..policy import FileAccessMode, remove_temp_dir

LEASE_MANIFEST_VERSION = 1


@dataclass(frozen=True, slots=True)
class CommandLease:
    job_id: str
    reservation_id: str
    logon_sid: str
    account_sid: str
    service_sid: str
    workspaces: tuple[str, ...]
    cwd: str
    temp_dir: str
    file_mode: str
    workspace_cap_sid: str
    temp_cap_sid: str
    capability_digest: str
    acl_entries: tuple[AclLeaseEntry, ...]


class CommandLeaseStore:
    """Persist exact ACE ownership so recovery never deletes pre-existing ACLs."""

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
        """Revoke only ACEs inserted by this job and preserve concurrent users."""

        with self._process_lock:
            values = self._read()
            stored = next((item for item in values if item.job_id == lease.job_id), None)
            if stored is None:
                return True
            remaining = [item for item in values if item.job_id != lease.job_id]
            checks: list[bool] = []
            for entry in reversed(stored.acl_entries):
                if not entry.owned:
                    continue
                if self._transfer_ownership(remaining, entry):
                    continue
                checks.append(self.acl_manager.revoke_entry(entry))
            checks.append(remove_temp_dir(Path(stored.temp_dir)))
            if all(checks):
                self._write(tuple(remaining))
                return True
            return False

    @staticmethod
    def _transfer_ownership(remaining: list[CommandLease], owner: AclLeaseEntry) -> bool:
        for lease_index, lease in enumerate(remaining):
            entries = list(lease.acl_entries)
            for entry_index, entry in enumerate(entries):
                if entry.signature != owner.signature:
                    continue
                if not entry.owned:
                    entries[entry_index] = replace(entry, owned=True)
                    remaining[lease_index] = replace(lease, acl_entries=tuple(entries))
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
            raise SandboxInitializationError(f"sandbox lease manifest is invalid: {self.path.resolve()}") from exc
        if (
            not isinstance(raw, dict)
            or raw.get("version") != LEASE_MANIFEST_VERSION
            or not isinstance(raw.get("leases"), list)
        ):
            raise SandboxInitializationError(f"sandbox lease manifest version is unsupported: {self.path.resolve()}")
        result: list[CommandLease] = []
        try:
            for item in raw["leases"]:
                if not isinstance(item, dict) or not isinstance(item.get("acl_entries"), list):
                    raise TypeError
                lease = CommandLease(
                    **{
                        **item,
                        "workspaces": tuple(item["workspaces"]),
                        "acl_entries": tuple(AclLeaseEntry(**entry) for entry in item["acl_entries"]),
                    }
                )
                FileAccessMode(lease.file_mode)
                if (
                    not lease.job_id
                    or not lease.reservation_id
                    or not lease.logon_sid
                    or not lease.account_sid
                    or not lease.service_sid
                    or not lease.workspaces
                    or not lease.cwd
                    or not lease.temp_dir
                    or not lease.workspace_cap_sid
                    or not lease.temp_cap_sid
                    or not lease.capability_digest
                ):
                    raise ValueError
                result.append(lease)
        except (KeyError, TypeError, ValueError) as exc:
            raise SandboxInitializationError(f"sandbox lease manifest is invalid: {self.path.resolve()}") from exc
        return tuple(result)

    def _write(self, values: tuple[CommandLease, ...]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}")
        payload = {"version": LEASE_MANIFEST_VERSION, "leases": [asdict(item) for item in values]}
        try:
            with temporary.open("w", encoding="utf-8", newline="\n") as stream:
                json.dump(payload, stream, sort_keys=True, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)


__all__ = ["CommandLease", "CommandLeaseStore", "LEASE_MANIFEST_VERSION"]
