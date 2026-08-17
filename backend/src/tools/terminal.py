"""Host terminal discovery and workspace path conversion."""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Mapping
from pathlib import Path

from backend.domain.terminal import (
    DEFAULT_TERMINAL_TYPE,
    TERMINAL_LABELS,
    TerminalType,
    normalize_terminal_type,
)

_DETECTION_TIMEOUT_SECONDS = 2


def _which(name: str, environment: Mapping[str, str]) -> str | None:
    return shutil.which(name, path=environment.get("PATH"))


def _existing_file(path: str | Path | None) -> str | None:
    if not path:
        return None
    candidate = Path(path)
    try:
        return str(candidate) if candidate.is_file() else None
    except OSError:
        return None


def _git_bash_executable(environment: Mapping[str, str]) -> str | None:
    candidates: list[str] = []
    for variable in ("ProgramFiles", "ProgramFiles(x86)", "LocalAppData"):
        root = environment.get(variable)
        if root:
            candidates.extend(
                [
                    str(Path(root) / "Git" / "bin" / "bash.exe"),
                    str(Path(root) / "Programs" / "Git" / "bin" / "bash.exe"),
                ]
            )
    candidates.extend(["bash.exe", "bash"])
    for candidate in candidates:
        resolved = _existing_file(candidate) or _which(candidate, environment)
        if resolved:
            return resolved
    return None


def terminal_executable(
    terminal_type: TerminalType | str,
    *,
    environment: Mapping[str, str] | None = None,
) -> str | None:
    """Resolve the executable used for a terminal, if it is discoverable."""

    env = os.environ if environment is None else environment
    selected = normalize_terminal_type(terminal_type)
    if selected == "cmd":
        return _existing_file(env.get("ComSpec")) or _which("cmd.exe", env) or "cmd.exe"
    if selected == "powershell":
        return _which("powershell.exe", env) or "powershell.exe"
    if selected == "pwsh":
        return _which("pwsh.exe", env) or "pwsh.exe"
    if selected == "git_bash":
        return _git_bash_executable(env)
    return _which("wsl.exe", env) or "wsl.exe"


def _wsl_executable(environment: Mapping[str, str]) -> str | None:
    executable = _which("wsl.exe", environment)
    if executable is None:
        return None
    try:
        result = subprocess.run(
            [executable, "--status"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=_DETECTION_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return executable if result.returncode == 0 else None


def available_terminal_executables(
    *,
    is_windows: bool | None = None,
    environment: Mapping[str, str] | None = None,
) -> dict[TerminalType, str]:
    """Return terminals that can be launched on the current host."""

    windows = os.name == "nt" if is_windows is None else is_windows
    if not windows:
        return {}
    env = os.environ if environment is None else environment
    result: dict[TerminalType, str] = {}
    cmd = _existing_file(env.get("ComSpec")) or _which("cmd.exe", env)
    if cmd:
        result["cmd"] = cmd
    powershell = _which("powershell.exe", env)
    if powershell:
        result["powershell"] = powershell
    pwsh = _which("pwsh.exe", env)
    if pwsh:
        result["pwsh"] = pwsh
    git_bash = _git_bash_executable(env)
    if git_bash:
        result["git_bash"] = git_bash
    wsl = _wsl_executable(env)
    if wsl:
        result["wsl"] = wsl
    return result


def available_terminal_options(
    *,
    is_windows: bool | None = None,
    environment: Mapping[str, str] | None = None,
) -> list[dict[str, str]]:
    """Return Ant Design-ready options in a stable order."""

    executables = available_terminal_executables(is_windows=is_windows, environment=environment)
    return [{"value": name, "label": TERMINAL_LABELS[name]} for name in executables]


def effective_terminal_type(
    value: object,
    *,
    is_windows: bool | None = None,
    environment: Mapping[str, str] | None = None,
) -> TerminalType:
    """Use the requested terminal when available, otherwise the safe default."""

    selected = normalize_terminal_type(value)
    windows = os.name == "nt" if is_windows is None else is_windows
    if not windows:
        return selected
    available = available_terminal_executables(is_windows=True, environment=environment)
    if selected in available:
        return selected
    if DEFAULT_TERMINAL_TYPE in available:
        return DEFAULT_TERMINAL_TYPE
    if available:
        return next(iter(available))
    return selected


def windows_workspace_to_wsl(workspace: Path) -> str:
    """Convert a local Windows workspace to WSL's mounted-drive path."""

    raw = str(workspace)
    if raw.startswith("\\\\"):
        raise ValueError("WSL does not support UNC workspace paths")
    drive, remainder = os.path.splitdrive(raw)
    if len(drive) != 2 or drive[1] != ":":
        raise ValueError("WSL requires a drive-letter workspace path")
    relative = remainder.replace("\\", "/").lstrip("/")
    return f"/mnt/{drive[0].lower()}/{relative}"
