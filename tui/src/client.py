"""Network client for the mini-agent backend (client tier).

Talks to the backend's HTTP/SSE API only; this module never imports backend
internals, so the TUI can run fully decoupled from the server code.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlsplit, urlunsplit

DEFAULT_SERVER = "http://127.0.0.1:8000"


def _normalize_server_url(value: str) -> str:
    """Use one stable key for the same server across CLI invocations."""
    raw = value.strip().rstrip("/")
    parsed = urlsplit(raw)
    if not parsed.scheme or not parsed.netloc:
        return raw
    hostname = (parsed.hostname or "").lower()
    try:
        port = parsed.port
    except ValueError:
        return raw
    default_port = (parsed.scheme.lower() == "http" and port == 80) or (
        parsed.scheme.lower() == "https" and port == 443
    )
    netloc = hostname
    if port and not default_port:
        netloc = f"{netloc}:{port}"
    return urlunsplit((parsed.scheme.lower(), netloc, parsed.path.rstrip("/"), "", ""))


class ApiError(RuntimeError):
    """A request to the backend failed or returned an error event."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class MiniAgentClient:
    def __init__(self, base_url: str = DEFAULT_SERVER, *, timeout: float = 30.0) -> None:
        self.base_url = _normalize_server_url(base_url)
        self.timeout = timeout

    def _request(
        self,
        method: str,
        path: str,
        body: dict | None = None,
        *,
        timeout: float | None = None,
    ) -> Any:
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"}
        request = urllib.request.Request(
            self.base_url + path,
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout or self.timeout) as response:
                payload = response.read().decode("utf-8")
                return json.loads(payload) if payload else None
        except urllib.error.HTTPError as exc:
            raise ApiError(f"HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')}", exc.code) from exc
        except urllib.error.URLError as exc:
            raise ApiError(f"无法连接后端 {self.base_url}: {exc.reason}") from exc

    def health(self) -> dict:
        return self._request("GET", "/api/health")

    def list_tools(self) -> list[dict]:
        return self._request("GET", "/api/tools")

    def list_skills(self) -> list[dict]:
        return self._request("GET", "/api/skills")

    def list_sidebar_threads(self) -> list[dict]:
        return self._request("GET", "/api/sidebar-threads")

    def post_decision(self, decision_id: str, decision: dict) -> None:
        self._request("POST", "/api/decisions", {**decision, "decision_id": decision_id})
