from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "tui" / "src"))

from tui.client import ApiError, MiniAgentClient  # noqa: E402


def test_device_authorization_opens_browser_and_reuses_keyring_token(monkeypatch) -> None:
    stored: dict[str, str] = {}
    opened: list[str] = []
    monkeypatch.setattr("tui.client.keyring.get_password", lambda service, key: stored.get(key))
    monkeypatch.setattr("tui.client.keyring.set_password", lambda service, key, value: stored.__setitem__(key, value))
    monkeypatch.setattr("tui.client.keyring.delete_password", lambda service, key: stored.pop(key, None))
    monkeypatch.setattr("tui.client.webbrowser.open", lambda url: opened.append(url))
    monkeypatch.setattr("tui.client.time.sleep", lambda seconds: None)

    class FakeClient(MiniAgentClient):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.polls = 0

        def _request(self, method, path, body=None, **kwargs):
            if path == "/api/auth/me":
                if self._token:
                    return {"user": {"id": "user", "email": "user@example.com"}}
                raise ApiError("unauthorized", 401)
            if path == "/api/auth/device/start":
                return {
                    "poll_secret": "p" * 64,
                    "verification_url": "http://localhost:5173/device/approve?grant=" + "g" * 64,
                    "expires_in": 10,
                    "poll_interval": 1,
                }
            if path == "/api/auth/device/token":
                self.polls += 1
                return {"status": "pending"} if self.polls == 1 else {"status": "approved", "access_token": "t" * 64}
            raise AssertionError(path)

    first = FakeClient("https://mini-agent.example")
    first.ensure_authenticated()
    assert opened and opened[0].startswith("http://localhost:5173/device/approve")
    assert stored["https://mini-agent.example"] == "t" * 64

    second = FakeClient("https://mini-agent.example")
    second.ensure_authenticated()
    assert second._token == "t" * 64
