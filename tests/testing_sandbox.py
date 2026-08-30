from __future__ import annotations

import subprocess
from collections.abc import Sequence
from typing import Any

from backend.sandbox import SandboxLauncher
from backend.sandbox.control.maintenance import SandboxMaintenanceGate


class DirectTestSandboxLauncher(SandboxLauncher):
    """Test-only process launcher for tests that do not exercise isolation."""

    def __init__(self) -> None:
        self.maintenance_gate = SandboxMaintenanceGate()
        self.policies: list[Any] = []
        self._temp_dirs: dict[int, object] = {}

    def command_lease(self):
        return self.maintenance_gate.acquire_command()

    def popen_factory(self, policy, **_options):
        self.policies.append(policy)

        def factory(argv: Sequence[str], **kwargs: Any):
            return subprocess.Popen(argv, **kwargs)

        return factory

    @staticmethod
    def terminate_tree(process: Any) -> None:
        process.terminate()

    @staticmethod
    def cleanup(_process_or_pid: Any) -> bool:
        return True
