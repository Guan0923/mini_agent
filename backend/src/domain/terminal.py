"""Stable terminal identifiers shared by settings, API, and execution."""

from __future__ import annotations

from typing import Literal

TerminalType = Literal["cmd", "git_bash", "powershell", "pwsh", "wsl"]

DEFAULT_TERMINAL_TYPE: TerminalType = "cmd"
TERMINAL_TYPES = frozenset({"cmd", "git_bash", "powershell", "pwsh", "wsl"})
TERMINAL_LABELS: dict[str, str] = {
    "cmd": "命令提示符（cmd）",
    "git_bash": "Git Bash",
    "powershell": "Windows PowerShell",
    "pwsh": "PowerShell 7（pwsh）",
    "wsl": "Windows Subsystem for Linux（WSL）",
}


def normalize_terminal_type(value: object, *, default: TerminalType = DEFAULT_TERMINAL_TYPE) -> TerminalType:
    """Validate a persisted terminal identifier without probing the host."""

    candidate = default if value is None or value == "" else value
    if not isinstance(candidate, str) or candidate not in TERMINAL_TYPES:
        raise ValueError("terminal_type must be cmd, git_bash, powershell, pwsh, or wsl")
    return candidate  # type: ignore[return-value]
