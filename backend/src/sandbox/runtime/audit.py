"""Fail-closed bounded audit for writable paths outside command allow roots."""

from __future__ import annotations

import ctypes
import os
import time
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from ..errors import SandboxInitializationError
from ..native_windows.security import WindowsAclManager
from ..policy import FileAccessMode

MAX_ITEMS_PER_DIRECTORY = 1000
MAX_CHECKED_PATHS = 50000
AUDIT_TIMEOUT_SECONDS = 2.0


class SandboxAuditFailure(StrEnum):
    SCAN_INCOMPLETE = "world_writable_scan_incomplete"
    PATH_UNPROTECTED = "world_writable_path_unprotected"
    CAPABILITY_ACL_FAILED = "capability_acl_failed"
    TOKEN_MODEL_MISMATCH = "token_model_mismatch"


class SandboxAuditError(SandboxInitializationError):
    def __init__(self, reason: SandboxAuditFailure, *, risk_paths: tuple[str, ...] = ()) -> None:
        super().__init__("sandbox_unavailable")
        self.reason = reason
        self.risk_paths = risk_paths[:5]


@dataclass(frozen=True, slots=True)
class WritablePathAudit:
    deny_paths: tuple[Path, ...]
    identities: dict[str, str]
    checked_paths: int


class WorldWritablePathAuditor:
    def __init__(
        self,
        acl_manager: WindowsAclManager,
        *,
        clock=None,
        max_items_per_directory: int = MAX_ITEMS_PER_DIRECTORY,
        max_checked_paths: int = MAX_CHECKED_PATHS,
        timeout_seconds: float = AUDIT_TIMEOUT_SECONDS,
    ) -> None:
        self.acl_manager = acl_manager
        self.clock = clock or time.monotonic
        self.max_items_per_directory = max_items_per_directory
        self.max_checked_paths = max_checked_paths
        self.timeout_seconds = timeout_seconds

    def scan(
        self,
        *,
        workspaces: tuple[Path, ...],
        temp_dir: Path,
        environment: Mapping[str, str],
        account_sid: str,
        file_mode: FileAccessMode,
    ) -> WritablePathAudit:
        if file_mode is FileAccessMode.FULL_ACCESS:
            return WritablePathAudit((), {}, 0)
        started = self.clock()
        everyone_sid = "S-1-1-0"
        seen_objects: set[str] = set()
        identities: dict[str, str] = {}
        deny_paths: list[Path] = []
        checked = 0
        allowed_roots = [temp_dir]
        if file_mode is FileAccessMode.WORKSPACE_WRITE:
            allowed_roots.extend(workspaces)
        allowed_identities = [self.acl_manager.path_identity(path) for path in allowed_roots]

        def audit_one(candidate: Path) -> None:
            nonlocal checked
            self._check_limits(started, checked)
            try:
                identity = self.acl_manager.path_identity(candidate)
            except SandboxInitializationError as exc:
                raise SandboxAuditError(
                    SandboxAuditFailure.SCAN_INCOMPLETE,
                    risk_paths=(str(candidate),),
                ) from exc
            if identity.object_id in seen_objects:
                return
            seen_objects.add(identity.object_id)
            checked += 1
            self._check_limits(started, checked)
            key = str(identity.path)
            identities[key] = identity.object_id
            try:
                writable = self.acl_manager.path_allows_write(identity.path, everyone_sid, account_sid)
            except SandboxInitializationError as exc:
                raise SandboxAuditError(
                    SandboxAuditFailure.SCAN_INCOMPLETE,
                    risk_paths=(str(identity.path),),
                ) from exc
            if not writable:
                return
            if any(_contains(root.path, identity.path) for root in allowed_identities):
                return
            deny_paths.append(identity.path)

        for root in self._candidate_roots(workspaces, temp_dir, environment):
            if not root.exists():
                continue
            audit_one(root)
            try:
                with os.scandir(root) as entries:
                    for index, entry in enumerate(entries, start=1):
                        if index >= self.max_items_per_directory:
                            raise SandboxAuditError(SandboxAuditFailure.SCAN_INCOMPLETE)
                        self._check_limits(started, checked)
                        try:
                            if entry.is_dir(follow_symlinks=True):
                                audit_one(Path(entry.path))
                        except OSError as exc:
                            raise SandboxAuditError(
                                SandboxAuditFailure.SCAN_INCOMPLETE,
                                risk_paths=(str(entry.path),),
                            ) from exc
            except SandboxAuditError:
                raise
            except OSError as exc:
                raise SandboxAuditError(
                    SandboxAuditFailure.SCAN_INCOMPLETE,
                    risk_paths=(str(root),),
                ) from exc
        return WritablePathAudit(tuple(deny_paths), identities, checked)

    def _check_limits(self, started: float, checked: int) -> None:
        if checked >= self.max_checked_paths or self.clock() - started >= self.timeout_seconds:
            raise SandboxAuditError(SandboxAuditFailure.SCAN_INCOMPLETE)

    @staticmethod
    def _candidate_roots(
        workspaces: tuple[Path, ...],
        temp_dir: Path,
        environment: Mapping[str, str],
    ) -> tuple[Path, ...]:
        raw: list[Path] = [*workspaces, temp_dir]
        for name in ("TEMP", "TMP", "USERPROFILE", "PUBLIC", "PROGRAMDATA"):
            value = environment.get(name) or os.environ.get(name)
            if value:
                raw.append(Path(value))
        path_value = environment.get("PATH") or os.environ.get("PATH") or ""
        raw.extend(Path(item) for item in os.pathsep.join([path_value]).split(os.pathsep) if item)
        windows = environment.get("WINDIR") or environment.get("SYSTEMROOT") or os.environ.get("WINDIR")
        if windows:
            raw.append(Path(windows))
        system_drive = environment.get("SYSTEMDRIVE") or os.environ.get("SYSTEMDRIVE")
        if system_drive:
            raw.append(Path(f"{system_drive}\\"))
        raw.extend(_fixed_volume_roots())
        result: list[Path] = []
        seen: set[str] = set()
        for candidate in raw:
            value = os.path.normcase(os.path.abspath(str(candidate)))
            if value not in seen:
                seen.add(value)
                result.append(Path(value))
        return tuple(result)


def verify_audit_identities(acl_manager: WindowsAclManager, identities: Mapping[str, str]) -> None:
    for path, expected in identities.items():
        current = acl_manager.path_identity(Path(path))
        if current.object_id != expected or os.path.normcase(str(current.path)) != os.path.normcase(path):
            raise SandboxAuditError(
                SandboxAuditFailure.SCAN_INCOMPLETE,
                risk_paths=(path,),
            )


def _contains(root: Path, candidate: Path) -> bool:
    try:
        return candidate == root or candidate.is_relative_to(root)
    except (OSError, ValueError):
        return False


def _fixed_volume_roots() -> tuple[Path, ...]:
    if os.name != "nt":
        return ()
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    mask = int(kernel.GetLogicalDrives())
    result: list[Path] = []
    for index in range(26):
        if not mask & (1 << index):
            continue
        root = f"{chr(ord('A') + index)}:\\"
        if int(kernel.GetDriveTypeW(ctypes.c_wchar_p(root))) == 3:  # DRIVE_FIXED
            result.append(Path(root))
    return tuple(result)


__all__ = [
    "AUDIT_TIMEOUT_SECONDS",
    "MAX_CHECKED_PATHS",
    "MAX_ITEMS_PER_DIRECTORY",
    "SandboxAuditError",
    "SandboxAuditFailure",
    "WorldWritablePathAuditor",
    "WritablePathAudit",
    "verify_audit_identities",
]
