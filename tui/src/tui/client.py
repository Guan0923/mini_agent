"""Network client for the mini-agent backend (client tier).

Talks to the backend's HTTP/SSE API only; this module never imports backend
internals, so the TUI can run fully decoupled from the server code.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Callable

EventSink = Callable[[dict], None]
DecisionResponder = Callable[[dict], dict]

DEFAULT_SERVER = "http://127.0.0.1:8000"


class ApiError(RuntimeError):
    """A request to the backend failed or returned an error event."""


class MiniAgentClient:
    def __init__(self, base_url: str = DEFAULT_SERVER, *, timeout: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _request(self, method: str, path: str, body: dict | None = None, *, timeout: float | None = None) -> Any:
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(
            self.base_url + path,
            data=data,
            headers={"Content-Type": "application/json"},
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout or self.timeout) as response:
                payload = response.read().decode("utf-8")
                return json.loads(payload) if payload else None
        except urllib.error.HTTPError as exc:
            raise ApiError(f"HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')}") from exc
        except urllib.error.URLError as exc:
            raise ApiError(f"无法连接后端 {self.base_url}: {exc.reason}") from exc

    def health(self) -> dict:
        return self._request("GET", "/api/health")

    def list_tools(self) -> list[dict]:
        return self._request("GET", "/api/tools")

    def list_skills(self) -> list[dict]:
        return self._request("GET", "/api/skills")

    def list_sessions(self) -> list[dict]:
        return self._request("GET", "/api/sessions")

    def post_decision(self, decision_id: str, decision: dict) -> None:
        self._request("POST", "/api/decisions", {**decision, "decision_id": decision_id})

    def run_task(
        self,
        prompt: str,
        *,
        on_event: EventSink,
        on_decision_requested: DecisionResponder | None = None,
        interactive: bool = False,
        timeout: float = 600.0,
    ) -> dict:
        """Stream one agent run over SSE and return the final ``done`` payload.

        ``on_decision_requested`` (if interactive) returns the decision dict to
        post back, e.g. ``{"choice": "continue"}``.
        """
        body = json.dumps({"prompt": prompt, "interactive": interactive}).encode()
        request = urllib.request.Request(
            self.base_url + "/api/chat",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            response = urllib.request.urlopen(request, timeout=timeout)
        except urllib.error.HTTPError as exc:
            raise ApiError(f"HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')}") from exc
        except urllib.error.URLError as exc:
            raise ApiError(f"无法连接后端 {self.base_url}: {exc.reason}") from exc

        done: dict = {}
        buffer = ""
        try:
            for raw in response:
                buffer += raw.decode("utf-8", "replace")
                while "\n\n" in buffer:
                    block, buffer = buffer.split("\n\n", 1)
                    for line in block.splitlines():
                        if not line.startswith("data: "):
                            continue
                        try:
                            message = json.loads(line[6:])
                        except ValueError:
                            continue
                        kind = message.get("type")
                        if kind == "event":
                            if message.get("kind") == "decision_requested" and on_decision_requested is not None:
                                decision = on_decision_requested(message.get("data", {}))
                                if decision:
                                    self.post_decision(message["data"]["decision_id"], decision)
                            on_event(message)
                        elif kind == "done":
                            done = message
                        elif kind == "error":
                            raise ApiError(message.get("message", "未知错误"))
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                close()
        return done
