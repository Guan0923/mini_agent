"""Network client for the mini-agent backend (client tier).

Talks to the backend's HTTP/SSE API only; this module never imports backend
internals, so the TUI can run fully decoupled from the server code.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
import webbrowser
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import keyring

EventSink = Callable[[dict], None]
DecisionResponder = Callable[[dict], dict]

DEFAULT_SERVER = "http://127.0.0.1:8000"


KEYRING_SERVICE = "mini-agent-device"


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
    def __init__(self, base_url: str = DEFAULT_SERVER, *, timeout: float = 30.0, token: str | None = None) -> None:
        self.base_url = _normalize_server_url(base_url)
        self.timeout = timeout
        self._token = token

    @property
    def _keyring_key(self) -> str:
        return self.base_url

    def _load_token(self) -> str | None:
        if self._token:
            return self._token
        try:
            self._token = keyring.get_password(KEYRING_SERVICE, self._keyring_key)
        except Exception:
            self._token = None
        return self._token

    def _save_token(self, token: str) -> None:
        self._token = token
        try:
            keyring.set_password(KEYRING_SERVICE, self._keyring_key, token)
        except Exception:
            print("[client] 系统凭据库不可用，本次令牌只保留在当前进程中。")

    def _forget_token(self) -> None:
        self._token = None
        try:
            keyring.delete_password(KEYRING_SERVICE, self._keyring_key)
        except Exception:
            pass

    def _request(
        self,
        method: str,
        path: str,
        body: dict | None = None,
        *,
        timeout: float | None = None,
        authenticated: bool = True,
        accepted_errors: tuple[int, ...] = (),
    ) -> Any:
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"}
        token = self._load_token() if authenticated else None
        if token:
            headers["Authorization"] = f"Bearer {token}"
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
            if exc.code == 401 and authenticated:
                self._forget_token()
            if exc.code in accepted_errors:
                payload = exc.read().decode("utf-8", "replace")
                return json.loads(payload) if payload else None
            raise ApiError(f"HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')}", exc.code) from exc
        except urllib.error.URLError as exc:
            raise ApiError(f"无法连接后端 {self.base_url}: {exc.reason}") from exc

    def ensure_authenticated(self) -> None:
        token = self._load_token()
        if token:
            try:
                self._request("GET", "/api/auth/me")
                return
            except ApiError as exc:
                if exc.status not in {401, 403}:
                    raise
                self._forget_token()

        started = self._request("POST", "/api/auth/device/start", {}, authenticated=False)
        verification_url = str(started["verification_url"])
        poll_secret = str(started["poll_secret"])
        expires_in = int(started.get("expires_in", 600))
        interval = max(1, int(started.get("poll_interval", 2)))
        print("[client] 未检测到有效登录，正在打开浏览器授权…")
        print(f"[client] 如果浏览器没有自动打开，请访问：{verification_url}")
        try:
            webbrowser.open(verification_url)
        except Exception:
            pass
        deadline = time.monotonic() + expires_in
        while time.monotonic() < deadline:
            try:
                result = self._request(
                    "POST",
                    "/api/auth/device/token",
                    {"poll_secret": poll_secret},
                    authenticated=False,
                    accepted_errors=(202,),
                )
            except ApiError as exc:
                if exc.status in {403, 410}:
                    raise ApiError("浏览器设备授权未完成或已拒绝。", exc.status) from exc
                raise
            if result and result.get("status") == "approved" and result.get("access_token"):
                self._save_token(str(result["access_token"]))
                print("[client] 浏览器授权成功，继续执行任务。")
                return
            time.sleep(interval)
        raise ApiError("浏览器设备授权已超时，请重新运行命令。", 408)

    def logout(self) -> None:
        if self._load_token():
            try:
                self._request("POST", "/api/auth/logout", {})
            except ApiError as exc:
                if exc.status not in {401, 403}:
                    raise
        self._forget_token()

    def health(self) -> dict:
        return self._request("GET", "/api/health")

    def list_tools(self) -> list[dict]:
        self.ensure_authenticated()
        return self._request("GET", "/api/tools")

    def list_skills(self) -> list[dict]:
        self.ensure_authenticated()
        return self._request("GET", "/api/skills")

    def list_sessions(self) -> list[dict]:
        self.ensure_authenticated()
        return self._request("GET", "/api/sessions")

    def post_decision(self, decision_id: str, decision: dict) -> None:
        self.ensure_authenticated()
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
        self.ensure_authenticated()
        body = json.dumps({"prompt": prompt, "interactive": interactive}).encode()
        headers = {"Content-Type": "application/json"}
        token = self._load_token()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        request = urllib.request.Request(
            self.base_url + "/api/chat",
            data=body,
            headers=headers,
            method="POST",
        )
        try:
            response = urllib.request.urlopen(request, timeout=timeout)
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                self._forget_token()
            raise ApiError(f"HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')}", exc.code) from exc
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
