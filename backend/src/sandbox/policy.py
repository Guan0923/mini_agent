"""Provider-neutral Windows command sandbox policy and validation."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from .errors import SandboxPolicyError


class PermissionMode(StrEnum):
    READ_ONLY = "read_only"
    WORKSPACE_WRITE = "workspace_write"
    FULL_ACCESS = "full_access"


class NetworkMode(StrEnum):
    NO_NETWORK = "no_network"
    RESTRICTED_NETWORK = "restricted_network"
    FULL_NETWORK = "full_network"


class TerminalKind(StrEnum):
    CMD = "cmd"
    POWERSHELL = "powershell"
    PWSH = "pwsh"
    GIT_BASH = "git_bash"


LEGACY_PERMISSION_MODES = frozenset({"approval_for_me", "full_access"})
SUPPORTED_TERMINALS = frozenset(item.value for item in TerminalKind)


def normalize_permission_mode(value: object, *, default: PermissionMode = PermissionMode.READ_ONLY) -> PermissionMode:
    """Map pre-sandbox permission values to the new three-level contract."""

    if value in {"approval_for_me", "read_only", None, ""}:
        return default if value in {None, ""} else PermissionMode.READ_ONLY
    if value == "workspace_write":
        return PermissionMode.WORKSPACE_WRITE
    if value == "full_access":
        return PermissionMode.FULL_ACCESS
    raise SandboxPolicyError("permission_mode is invalid")


def migrate_legacy_permission_mode(value: object) -> PermissionMode:
    """Migrate persisted pre-sandbox values before interpreting new input.

    The old ``full_access`` switch did not carry the mandatory joint file and
    network confirmation, so it is intentionally downgraded. New API input
    should use :func:`normalize_permission_mode` after migration.
    """

    if value in {"approval_for_me", "full_access", None, ""}:
        return PermissionMode.READ_ONLY
    return normalize_permission_mode(value)


@dataclass(frozen=True, slots=True)
class NetworkRule:
    host: str
    port: int

    def __post_init__(self) -> None:
        host = self.host.strip().lower()
        if not host or len(host) > 253 or any(ch.isspace() for ch in host):
            raise SandboxPolicyError("network host is invalid")
        if isinstance(self.port, bool) or not isinstance(self.port, int) or not 1 <= self.port <= 65535:
            raise SandboxPolicyError("network port must be between 1 and 65535")
        object.__setattr__(self, "host", host)


@dataclass(frozen=True, slots=True)
class ResolvedNetworkRule:
    address: str
    port: int


@dataclass(frozen=True, slots=True)
class SandboxLimits:
    """Per-job limits. Values are intentionally bounded before admission."""

    wall_seconds: int = 300
    cpu_seconds: int = 300
    memory_mib: int = 4096
    processes: int = 256
    handles: int = 16384
    output_chars: int = 20000
    disk_mib: int = 0

    def validate(self) -> None:
        ranges = {
            "wall_seconds": (1, 300),
            "cpu_seconds": (1, 300),
            "memory_mib": (128, 4096),
            "processes": (1, 256),
            "handles": (64, 16384),
            "output_chars": (1000, 20000),
        }
        for name, (minimum, maximum) in ranges.items():
            value = getattr(self, name)
            if isinstance(value, bool) or not minimum <= value <= maximum:
                raise SandboxPolicyError(f"{name} must be between {minimum} and {maximum}")
        if isinstance(self.disk_mib, bool) or not (self.disk_mib == 0 or 1 <= self.disk_mib <= 20 * 1024):
            raise SandboxPolicyError("disk_mib must be 0 or between 1 and 20480")

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object] | None) -> SandboxLimits:
        values = dict(raw or {})
        aliases = {"wall_clock_seconds": "wall_seconds", "memory_mb": "memory_mib", "max_processes": "processes"}
        for source, target in aliases.items():
            if source in values and target not in values:
                values[target] = values[source]
        result = cls(**{name: values.get(name, getattr(cls(), name)) for name in cls.__dataclass_fields__})
        result.validate()
        return result

    def to_dict(self) -> dict[str, int]:
        return {name: int(getattr(self, name)) for name in self.__dataclass_fields__}


_DEFAULT_ENVIRONMENT = frozenset(
    {
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "WINDIR",
        "SYSTEMDRIVE",
        "COMSPEC",
        "PROGRAMFILES",
        "PROGRAMFILES(X86)",
        "PROGRAMDATA",
        "LOCALAPPDATA",
        "APPDATA",
        "HOMEDRIVE",
        "HOMEPATH",
        "USERNAME",
        "USERDOMAIN",
        "NUMBER_OF_PROCESSORS",
        "PROCESSOR_ARCHITECTURE",
        "PROCESSOR_IDENTIFIER",
        "OS",
        "LANG",
        "LANGUAGE",
        "PYTHONIOENCODING",
        "PYTHONUTF8",
    }
)
_MIN_FREE_DISK_BYTES = 10 * 1024 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class SandboxPolicy:
    workspace: Path
    session_id: str
    job_id: str
    file_mode: PermissionMode = PermissionMode.READ_ONLY
    network_mode: NetworkMode = NetworkMode.NO_NETWORK
    network_allowlist: tuple[NetworkRule, ...] = ()
    limits: SandboxLimits = field(default_factory=SandboxLimits)
    terminal: TerminalKind = TerminalKind.CMD
    enforced: bool = True
    full_access_acknowledged: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "workspace", Path(self.workspace))
        if not isinstance(self.file_mode, PermissionMode):
            object.__setattr__(self, "file_mode", normalize_permission_mode(self.file_mode))
        if not isinstance(self.network_mode, NetworkMode):
            try:
                object.__setattr__(self, "network_mode", NetworkMode(str(self.network_mode)))
            except ValueError as exc:
                raise SandboxPolicyError("network_mode is invalid") from exc
        if not isinstance(self.terminal, TerminalKind):
            try:
                object.__setattr__(self, "terminal", TerminalKind(str(self.terminal)))
            except ValueError as exc:
                raise SandboxPolicyError("terminal is invalid") from exc
        # Keep construction platform-neutral for policy/hash unit tests. The
        # launcher performs the authoritative Windows availability check at the
        # process boundary with its injected platform value.
        self.validate(is_windows=True)

    def validate(self, *, is_windows: bool | None = None) -> None:
        windows = os.name == "nt" if is_windows is None else is_windows
        if not windows:
            raise SandboxPolicyError("Windows sandbox is unavailable on this platform")
        if not self.session_id or not self.job_id:
            raise SandboxPolicyError("session_id and job_id are required")
        if self.terminal.value not in SUPPORTED_TERMINALS:
            raise SandboxPolicyError("unsupported terminal")
        if self.file_mode is PermissionMode.FULL_ACCESS:
            if self.network_mode is not NetworkMode.FULL_NETWORK or not self.full_access_acknowledged:
                raise SandboxPolicyError("full_access requires full_network acknowledgement")
            if self.enforced:
                raise SandboxPolicyError("full_access must be explicitly marked non-sandbox")
        if self.network_mode is NetworkMode.RESTRICTED_NETWORK and not self.network_allowlist:
            raise SandboxPolicyError("restricted_network requires an allowlist")
        self.limits.validate()
        workspace = _safe_directory(self.workspace)
        if workspace != self.workspace.resolve():
            raise SandboxPolicyError("workspace path must resolve to a regular directory")

    def to_dict(self) -> dict[str, object]:
        return {
            "workspace": str(self.workspace),
            "session_id": self.session_id,
            "job_id": self.job_id,
            "file_mode": self.file_mode.value,
            "network_mode": self.network_mode.value,
            "network_allowlist": [{"host": rule.host, "port": rule.port} for rule in self.network_allowlist],
            "limits": self.limits.to_dict(),
            "terminal": self.terminal.value,
            "enforced": self.enforced,
            "full_access_acknowledged": self.full_access_acknowledged,
        }

    def policy_hash(self) -> str:
        payload = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
        return hashlib.sha256(payload).hexdigest()

    def environment(self, source: Mapping[str, str], *, temp_dir: Path) -> dict[str, str]:
        """Build an explicit, redacted environment for one job."""

        result: dict[str, str] = {}
        for name, value in source.items():
            upper = str(name).upper()
            if upper not in _DEFAULT_ENVIRONMENT and not upper.startswith("LC_"):
                continue
            if any(secret in upper for secret in ("TOKEN", "SECRET", "PASSWORD", "API_KEY", "CREDENTIAL", "COOKIE")):
                continue
            result[str(name)] = str(value)
        temp = str(temp_dir)
        temp_drive = temp_dir.drive
        result.pop("HOMEDRIVE", None)
        result.pop("HOMEPATH", None)
        result.update(
            {
                "TEMP": temp,
                "TMP": temp,
                "USERPROFILE": temp,
                "HOME": temp,
                "APPDATA": temp,
                "LOCALAPPDATA": temp,
            }
        )
        if temp_drive:
            result["HOMEDRIVE"] = temp_drive
            result["HOMEPATH"] = str(temp_dir).removeprefix(temp_drive) or "\\"
        return result

    def create_temp_dir(self, root: Path | None = None) -> Path:
        # Keep job scratch space outside the workspace. This is required for
        # read-only jobs and also prevents a child from using its temp tree as
        # an alternate write channel into the checked-out project.
        session_component = hashlib.sha256(self.session_id.encode("utf-8", errors="replace")).hexdigest()[:24]
        job_component = hashlib.sha256(self.job_id.encode("utf-8", errors="replace")).hexdigest()[:16]
        base = (
            Path(root) if root is not None else Path(tempfile.gettempdir()) / "mini-agent-sandbox" / session_component
        )
        if _has_reparse_ancestor(base):
            raise SandboxPolicyError("job temp root cannot be a reparse point")
        base.mkdir(parents=True, exist_ok=True)
        if _has_reparse_ancestor(base):
            raise SandboxPolicyError("job temp root cannot be a reparse point")
        result = Path(tempfile.mkdtemp(prefix=f"job-{job_component}-", dir=base))
        if _is_reparse_point(result):
            remove_temp_dir(result)
            raise SandboxPolicyError("job temp directory cannot be a reparse point")
        return result


def ensure_disk_reserve(path: Path, *, required_bytes: int = 0) -> None:
    """Keep the machine-wide free-space reserve before admitting a Job."""

    if isinstance(required_bytes, bool) or not isinstance(required_bytes, int) or required_bytes < 0:
        raise SandboxPolicyError("required disk bytes must be a non-negative integer")
    try:
        usage = shutil.disk_usage(path)
    except OSError as exc:
        raise SandboxPolicyError("disk space could not be inspected") from exc
    reserve = max(_MIN_FREE_DISK_BYTES, int(usage.total * 0.10))
    if usage.free - required_bytes < reserve:
        raise SandboxPolicyError("machine disk reserve is unavailable")


def _safe_directory(path: Path) -> Path:
    try:
        if _is_reparse_point(path) or not path.is_dir():
            raise SandboxPolicyError("workspace must be a regular directory")
        current = path
        while True:
            if _is_reparse_point(current):
                raise SandboxPolicyError("workspace cannot contain a reparse point")
            if current == current.parent:
                break
            current = current.parent
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise SandboxPolicyError("workspace cannot be inspected") from exc
    return resolved


def _is_reparse_point(path: Path) -> bool:
    if path.is_symlink():
        return True
    if os.name != "nt":
        return False
    try:
        import ctypes

        attributes = ctypes.windll.kernel32.GetFileAttributesW(str(path))
        return attributes != 0xFFFFFFFF and bool(attributes & 0x400)
    except (AttributeError, OSError):
        return False


def _has_reparse_ancestor(path: Path) -> bool:
    """Reject a path whose existing parents include a junction or symlink."""

    current = path
    while True:
        if _is_reparse_point(current):
            return True
        parent = current.parent
        if parent == current:
            return False
        current = parent


def remove_temp_dir(path: Path) -> bool:
    """Remove a job temp directory without following links."""

    try:
        if _is_reparse_point(path):
            path.unlink()
            return True
        if not path.exists():
            return True
        shutil.rmtree(path)
        return True
    except OSError:
        return False


def resolve_network_rules(
    rules: tuple[NetworkRule, ...],
    *,
    resolver=None,
) -> tuple[ResolvedNetworkRule, ...]:
    """Resolve restricted-network names before a Job enters the sandbox."""

    resolve = resolver or socket.getaddrinfo
    result: set[tuple[str, int]] = set()
    for rule in rules:
        try:
            answers = resolve(rule.host, rule.port, type=socket.SOCK_STREAM)
        except OSError as exc:
            raise SandboxPolicyError("network hostname could not be resolved") from exc
        for answer in answers:
            sockaddr = answer[4] if len(answer) > 4 else None
            address = sockaddr[0] if isinstance(sockaddr, tuple) and sockaddr else None
            if isinstance(address, str) and address:
                result.add((address, rule.port))
    if not result:
        raise SandboxPolicyError("network allowlist resolved to no addresses")
    return tuple(ResolvedNetworkRule(address, port) for address, port in sorted(result))


__all__ = [
    "LEGACY_PERMISSION_MODES",
    "NetworkMode",
    "NetworkRule",
    "ResolvedNetworkRule",
    "PermissionMode",
    "SandboxLimits",
    "SandboxPolicy",
    "TerminalKind",
    "normalize_permission_mode",
    "migrate_legacy_permission_mode",
    "remove_temp_dir",
    "resolve_network_rules",
    "ensure_disk_reserve",
]
