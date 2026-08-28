import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.domain.terminal import normalize_terminal_type
from backend.tools.terminal import (
    available_terminal_executables,
    effective_terminal_type,
    windows_workspace_to_wsl,
)


@pytest.mark.skipif(os.name != "nt", reason="Windows WSL path mapping test")
def test_windows_workspace_to_wsl_maps_drive_and_special_characters() -> None:
    assert windows_workspace_to_wsl(Path("C:/Users/测试用户/mini agent")) == "/mnt/c/Users/测试用户/mini agent"


@pytest.mark.parametrize("workspace", [Path("/tmp/workspace"), Path("\\\\server\\share\\workspace")])
def test_windows_workspace_to_wsl_rejects_non_drive_paths(workspace: Path) -> None:
    with pytest.raises(ValueError):
        windows_workspace_to_wsl(workspace)


def test_terminal_detection_returns_only_successful_windows_terminals(monkeypatch) -> None:
    found = {
        "cmd.exe": "C:/Windows/System32/cmd.exe",
        "powershell.exe": "C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe",
        "pwsh.exe": "C:/Program Files/PowerShell/7/pwsh.exe",
        "bash.exe": "C:/Program Files/Git/bin/bash.exe",
        "wsl.exe": "C:/Windows/System32/wsl.exe",
    }
    monkeypatch.setattr("backend.tools.terminal.shutil.which", lambda name, path=None: found.get(name))
    monkeypatch.setattr(
        "backend.tools.terminal.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0),
    )

    result = available_terminal_executables(is_windows=True, environment={"PATH": ""})

    assert list(result) == ["cmd", "powershell", "pwsh", "git_bash", "wsl"]


def test_terminal_detection_omits_wsl_when_status_check_fails(monkeypatch) -> None:
    monkeypatch.setattr(
        "backend.tools.terminal.shutil.which",
        lambda name, path=None: {
            "cmd.exe": "C:/Windows/System32/cmd.exe",
            "wsl.exe": "C:/Windows/System32/wsl.exe",
        }.get(name),
    )
    monkeypatch.setattr(
        "backend.tools.terminal.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(returncode=1),
    )

    result = available_terminal_executables(is_windows=True, environment={"PATH": ""})

    assert "wsl" not in result
    assert "cmd" in result


def test_effective_terminal_type_falls_back_to_cmd(monkeypatch) -> None:
    monkeypatch.setattr(
        "backend.tools.terminal.available_terminal_executables",
        lambda **kwargs: {"cmd": "cmd.exe", "pwsh": "pwsh.exe"},
    )

    assert effective_terminal_type("wsl", is_windows=True, environment={}) == "cmd"


def test_normalize_terminal_type_rejects_unknown_values() -> None:
    with pytest.raises(ValueError, match="terminal_type"):
        normalize_terminal_type("bash")
