from __future__ import annotations

import pytest

from backend.api.state import WebAppState
from backend.storage.message_queue import MemoryMessageQueue


@pytest.fixture(autouse=True)
def use_in_memory_message_queue(monkeypatch: pytest.MonkeyPatch) -> None:
    original = WebAppState.__init__

    def init(self, *args, **kwargs):
        kwargs.setdefault("message_queue", MemoryMessageQueue())
        original(self, *args, **kwargs)

    monkeypatch.setattr(WebAppState, "__init__", init)
