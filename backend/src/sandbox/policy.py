"""Provider-neutral Windows command sandbox policy and validation."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import shutil
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from .errors import SandboxPolicyError


class FileAccessMode(StrEnum):
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


SUPPORTED_TERMINALS = frozenset(item.value for item in TerminalKind)


PermissionMode = FileAccessMode
"""Compatibility alias for the pre-stage-two public name."""


def normalize_permission_mode(value: object, *, default: FileAccessMode = FileAccessMode.READ_ONLY) -> FileAccessMode:
    """Validate the three-level permission contract."""

    if value in {"read_only", None, ""}:
        return default if value in {None, ""} else FileAccessMode.READ_ONLY
    if value == "workspace_write":
        return FileAccessMode.WORKSPACE_WRITE
    if value == "full_access":
        return FileAccessMode.FULL_ACCESS
    raise SandboxPolicyError("permission_mode is invalid")


@dataclass(frozen=True, slots=True)
class NetworkRule:
    host: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "host", canonical_network_host(self.host))


def canonical_network_host(value: str) -> str:
    """Canonicalize one exact IP or hostname allowlist target."""

    host = value.strip().rstrip(".").casefold()
    if not host or len(host) > 253 or any(ch.isspace() for ch in host) or "*" in host or "/" in host:
        raise SandboxPolicyError("network host is invalid")
    try:
        return str(ipaddress.ip_address(host.split("%", 1)[0]))
    except ValueError:
        try:
            canonical = host.encode("idna").decode("ascii")
        except UnicodeError as exc:
            raise SandboxPolicyError("network host is invalid") from exc
        if not canonical or len(canonical) > 253 or any(not label for label in canonical.split(".")):
            raise SandboxPolicyError("network host is invalid")
        return canonical


@dataclass(frozen=True, slots=True)
class ResourceLimits:
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
    def from_mapping(cls, raw: Mapping[str, object] | None) -> ResourceLimits:
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


SandboxLimits = ResourceLimits
"""Compatibility alias for the pre-stage-two public name."""


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
        "LOCALAPPDATA",
        "APPDATA",
        "HOMEDRIVE",
        "HOMEPATH",
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
    workspaces: tuple[Path, ...]
    session_id: str
    job_id: str
    file_mode: FileAccessMode = FileAccessMode.READ_ONLY
    network_mode: NetworkMode = NetworkMode.NO_NETWORK
    network_allowlist: tuple[NetworkRule, ...] = ()
    limits: ResourceLimits = field(default_factory=ResourceLimits)
    terminal: TerminalKind = TerminalKind.CMD
    proxy_port: int = 17831

    def __post_init__(self) -> None:
        normalized_workspaces: list[Path] = []
        for raw_workspace in self.workspaces:
            workspace = Path(raw_workspace).resolve()
            if workspace not in normalized_workspaces:
                normalized_workspaces.append(workspace)
        if not normalized_workspaces:
            raise SandboxPolicyError("at least one workspace is required")
        object.__setattr__(self, "workspaces", tuple(normalized_workspaces))
        if not isinstance(self.file_mode, FileAccessMode):
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
        if self.network_mode is NetworkMode.RESTRICTED_NETWORK and not self.network_allowlist:
            raise SandboxPolicyError("restricted_network requires an allowlist")
        if len(self.network_allowlist) > 64:
            raise SandboxPolicyError("network allowlist may contain at most 64 rules")
        if (
            isinstance(self.proxy_port, bool)
            or not isinstance(self.proxy_port, int)
            or not 1 <= self.proxy_port <= 65535
        ):
            raise SandboxPolicyError("proxy_port must be between 1 and 65535")
        self.limits.validate()
        for workspace in self.workspaces:
            safe_workspace = _safe_directory(workspace)
            if safe_workspace != workspace.resolve():
                raise SandboxPolicyError("workspace path must resolve to a regular directory")

    def to_dict(self) -> dict[str, object]:
        return {
            "workspaces": [str(workspace) for workspace in self.workspaces],
            "session_id": self.session_id,
            "job_id": self.job_id,
            "file_mode": self.file_mode.value,
            "network_mode": self.network_mode.value,
            "network_allowlist": [{"host": rule.host} for rule in self.network_allowlist],
            "limits": self.limits.to_dict(),
            "terminal": self.terminal.value,
            "proxy_port": self.proxy_port,
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
        if root is not None:
            base = Path(root).resolve(strict=True)
        else:
            temp_root = Path(tempfile.gettempdir()).resolve(strict=True)
            base = temp_root / "mini-agent-sandbox" / session_component
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


@dataclass(frozen=True, slots=True)
class SandboxJobContext:
    """Immutable ownership and policy snapshot captured before Job launch."""

    user_id: str
    policy: SandboxPolicy
    job_kind: str = "command"

    def __post_init__(self) -> None:
        if not isinstance(self.user_id, str) or not self.user_id:
            raise SandboxPolicyError("sandbox user_id is required")
        if self.job_kind not in {"command", "mcp"}:
            raise SandboxPolicyError("sandbox job_kind must be command or mcp")

    def to_dict(self) -> dict[str, object]:
        return {
            "user_id": self.user_id,
            "job_kind": self.job_kind,
            "policy": self.policy.to_dict(),
        }


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
        _reject_reparse_descendants(resolved)
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
        # ctypes may expose INVALID_FILE_ATTRIBUTES as signed -1 rather than
        # the unsigned Win32 value 0xFFFFFFFF.
        if attributes in {-1, 0xFFFFFFFF}:
            return False
        return bool(attributes & 0x400)
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


def _reject_reparse_descendants(root: Path) -> None:
    """Fail closed if an existing workspace entry redirects path traversal."""

    pending = [root]
    while pending:
        current = pending.pop()
        with os.scandir(current) as entries:
            for entry in entries:
                child = Path(entry.path)
                if _is_reparse_point(child):
                    raise SandboxPolicyError("workspace cannot contain a reparse point")
                if entry.is_dir(follow_symlinks=False):
                    pending.append(child)


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


__all__ = [
    "FileAccessMode",
    "NetworkMode",
    "NetworkRule",
    "PermissionMode",
    "ResourceLimits",
    "SandboxLimits",
    "SandboxJobContext",
    "SandboxPolicy",
    "TerminalKind",
    "canonical_network_host",
    "normalize_permission_mode",
    "remove_temp_dir",
    "ensure_disk_reserve",
]
