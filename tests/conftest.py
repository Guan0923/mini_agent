from __future__ import annotations

import pytest

from backend.api.state import WebAppState
from backend.runtime.application import factory
from backend.sandbox import SandboxLauncher
from backend.storage.message_queue import MemoryMessageQueue


@pytest.fixture
def local_sandbox_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    """Use the explicit local test launcher when a test does not exercise Broker isolation."""

    monkeypatch.setattr(
        factory,
        "_sandbox_runtime",
        lambda _config, **_kwargs: (
            SandboxLauncher(is_windows=False, allow_local_backend=True),
            {},
        ),
    )


@pytest.fixture(autouse=True)
def use_in_memory_message_queue(monkeypatch: pytest.MonkeyPatch) -> None:
    original = WebAppState.__init__

    def init(self, *args, **kwargs):
        kwargs.setdefault("message_queue", MemoryMessageQueue())
        original(self, *args, **kwargs)

    monkeypatch.setattr(WebAppState, "__init__", init)
