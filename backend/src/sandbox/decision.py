"""Immutable Sandbox execution decision produced before a tool call."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .launcher import SandboxLauncher
from .policy import NetworkMode, PermissionMode, SandboxLimits, SandboxPolicy, TerminalKind


@dataclass(frozen=True, slots=True)
class SandboxExecutionDecision:
    """Fully approved inputs needed to construct one command job policy."""

    launcher: SandboxLauncher
    workspace: Path
    session_id: str
    user_id: str
    limits: SandboxLimits

    def command_policy(self, job_id: str, terminal: TerminalKind) -> SandboxPolicy:
        return SandboxPolicy(
            workspace=self.workspace,
            session_id=self.session_id,
            job_id=job_id,
            file_mode=PermissionMode.FULL_ACCESS,
            network_mode=NetworkMode.FULL_NETWORK,
            limits=self.limits,
            terminal=terminal,
            enforced=False,
            full_access_acknowledged=True,
        )


__all__ = ["SandboxExecutionDecision"]
