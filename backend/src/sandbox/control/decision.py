"""Immutable Sandbox execution decision produced before a tool call."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..policy import NetworkMode, NetworkRule, PermissionMode, SandboxLimits, SandboxPolicy, TerminalKind
from ..runtime.launcher import SandboxLauncher


@dataclass(frozen=True, slots=True)
class SandboxExecutionDecision:
    """Fully approved inputs needed to construct one command job policy."""

    launcher: SandboxLauncher
    workspace: Path
    session_id: str
    user_id: str
    file_mode: PermissionMode
    network_mode: NetworkMode
    network_allowlist: tuple[NetworkRule, ...]
    proxy_port: int
    limits: SandboxLimits

    def command_policy(self, job_id: str, terminal: TerminalKind) -> SandboxPolicy:
        return SandboxPolicy(
            workspace=self.workspace,
            session_id=self.session_id,
            job_id=job_id,
            file_mode=self.file_mode,
            network_mode=self.network_mode,
            network_allowlist=self.network_allowlist,
            limits=self.limits,
            terminal=terminal,
            proxy_port=self.proxy_port,
        )


__all__ = ["SandboxExecutionDecision"]
