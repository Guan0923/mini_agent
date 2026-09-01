"""Windows Broker ACL identities, command plans, and source grants."""

from __future__ import annotations

import hashlib
import os
import re
import stat
import struct
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path


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


def _service_class_command(service_name: str, service_class: str) -> list[str]:
    return [
        "reg.exe",
        "add",
        rf"HKLM\SYSTEM\CurrentControlSet\Services\{service_name}\PythonClass",
        "/ve",
        "/t",
        "REG_SZ",
        "/d",
        service_class,
        "/f",
    ]


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


def _sensitive_file_acl_commands(path: Path, service_name: str) -> list[list[str]]:
    return [
        ["takeown.exe", "/F", str(path), "/A"],
        [
            "icacls.exe",
            str(path),
            "/inheritance:r",
            "/grant:r",
            "SYSTEM:(F)",
            "Administrators:(F)",
            f"{_icacls_sid(_service_sid(service_name))}:(R)",
            "/C",
        ],
    ]


def _directory_contains(path: Path, name: str) -> bool:
    try:
        with os.scandir(path) as entries:
            return any(entry.name.casefold() == name.casefold() for entry in entries)
    except OSError as exc:
        raise OSError("Broker ProgramData directory is unavailable") from exc


@dataclass(frozen=True, slots=True)
class _SourceAclGrant:
    path: Path
    sid: str
    rights: str
    inherit: bool
    existing_children: bool = False

    def runner_command(self) -> list[str]:
        mode = "tree" if self.existing_children else "inherit" if self.inherit else "direct"
        return ["win32-acl", str(self.path), self.sid, self.rights, mode]


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
    relative = path.relative_to(boundary)
    ancestors = [boundary]
    current = boundary
    for component in relative.parts[:-1]:
        current /= component
        ancestors.append(current)
    return [
        *(_SourceAclGrant(ancestor, service_sid, "X", False) for ancestor in ancestors),
        _SourceAclGrant(path, service_sid, "RX", True, existing_children=True),
    ]


def _runtime_acl_grants(paths: Sequence[Path], service_executable: Path, service_name: str) -> list[_SourceAclGrant]:
    resolved_paths = tuple(Path(path).resolve() for path in paths)
    executable = Path(service_executable).resolve()
    if resolved_paths and not any(path == executable or path in executable.parents for path in resolved_paths):
        raise ValueError("Broker executable is outside the declared runtime paths")
    service_sid = _service_sid(service_name)
    unique_paths = dict.fromkeys(resolved_paths)
    return [_SourceAclGrant(path, service_sid, "RX", True, existing_children=True) for path in unique_paths]


def _is_reparse_point(path: Path) -> bool:
    """Return whether ``path`` redirects traversal outside its lexical tree."""

    metadata = path.lstat()
    attributes = int(getattr(metadata, "st_file_attributes", 0))
    reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return stat.S_ISLNK(metadata.st_mode) or bool(attributes & reparse_flag)


def _iter_acl_tree(root: Path) -> Iterator[tuple[Path, bool]]:
    """Yield an existing ACL tree without following reparse points."""

    resolved = Path(root)
    if not resolved.is_absolute() or len(resolved.parts) < 3:
        raise ValueError("Broker ACL tree path is invalid")
    if _is_reparse_point(resolved):
        raise ValueError("Broker ACL tree root cannot be a reparse point")
    if not resolved.is_dir():
        raise OSError("Broker ACL tree root is unavailable")

    yield resolved, True
    pending = [resolved]
    while pending:
        directory = pending.pop()
        try:
            with os.scandir(directory) as iterator:
                entries = sorted(iterator, key=lambda entry: entry.name.casefold(), reverse=True)
        except OSError as exc:
            raise OSError("Broker ACL tree could not be enumerated") from exc
        for entry in entries:
            child = Path(entry.path)
            try:
                if _is_reparse_point(child):
                    continue
                is_directory = entry.is_dir(follow_symlinks=False)
            except OSError as exc:
                raise OSError("Broker ACL tree entry could not be inspected") from exc
            yield child, is_directory
            if is_directory:
                pending.append(child)


def _apply_acl_target(path: Path, sid_text: str, rights: str, *, inherit: bool) -> None:
    try:
        import ntsecuritycon  # type: ignore[import-not-found]
        import win32security  # type: ignore[import-not-found]

        sid = win32security.ConvertStringSidToSid(sid_text)
        descriptor = win32security.GetNamedSecurityInfo(
            str(path),
            win32security.SE_FILE_OBJECT,
            win32security.DACL_SECURITY_INFORMATION,
        )
        dacl = descriptor.GetSecurityDescriptorDacl() or win32security.ACL()
        inheritance = win32security.CONTAINER_INHERIT_ACE | win32security.OBJECT_INHERIT_ACE if inherit else 0
        access = (
            ntsecuritycon.FILE_GENERIC_READ | ntsecuritycon.FILE_GENERIC_EXECUTE
            if rights == "RX"
            else ntsecuritycon.FILE_TRAVERSE
        )
        inherited_ace = int(getattr(win32security, "INHERITED_ACE", 0x10))
        for index in range(dacl.GetAceCount()):
            ace = dacl.GetAce(index)
            if (
                ace[0][0] == win32security.ACCESS_ALLOWED_ACE_TYPE
                and ace[2] == sid
                and ace[1] & access == access
                and ace[0][1] & inheritance == inheritance
                and not ace[0][1] & inherited_ace
            ):
                return
        dacl.AddAccessAllowedAceEx(win32security.ACL_REVISION_DS, inheritance, access, sid)
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
        raise OSError("Broker source ACL could not be configured") from exc


def _apply_source_acl_grant(grant: _SourceAclGrant) -> None:
    targets = _iter_acl_tree(grant.path) if grant.existing_children else ((grant.path, grant.path.is_dir()),)
    for path, is_directory in targets:
        _apply_acl_target(
            path,
            grant.sid,
            grant.rights,
            inherit=bool(grant.inherit and is_directory),
        )


def _secure_source_code(path: Path | None, boundary: Path | None, service_name: str) -> None:
    if path is None or boundary is None:
        return
    for grant in _source_acl_grants(path, boundary, service_name):
        _apply_source_acl_grant(grant)


__all__ = [
    "_icacls_sid",
    "_iter_acl_tree",
    "_managed_file_acl_commands",
    "_program_data_acl_commands",
    "_runtime_acl_grants",
    "_secure_source_code",
    "_sensitive_file_acl_commands",
    "_service_class_command",
    "_service_sid",
    "_sid_acl_command",
    "_source_acl_grants",
]
