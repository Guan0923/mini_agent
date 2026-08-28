from __future__ import annotations

import pytest

from backend.runtime.application import factory
from backend.sandbox import SandboxLauncher


@pytest.fixture
def local_sandbox_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    """Use the explicit local test launcher when a test does not exercise Broker isolation."""

    monkeypatch.setattr(
        factory,
        "_sandbox_runtime",
        lambda _config: (
            SandboxLauncher(is_windows=False, allow_local_backend=True),
            {},
        ),
    )
