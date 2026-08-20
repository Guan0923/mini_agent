"""Small HTTP client for the cloud control plane.

This module is intentionally separate from the model HTTP transport. Cloud
requests have different authentication, retry, and ownership semantics.
"""

from __future__ import annotations

import base64
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode, urlsplit

import requests

from backend.storage.auth.types import UserIdentity


class CloudSyncConflict(RuntimeError):
    """The cloud head changed before this event batch was accepted."""


class CloudApiError(RuntimeError):
    """An HTTP or protocol error returned by cloud."""

    def __init__(self, message: str, *, status_code: int | None = None, retryable: bool = False) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable


class CloudUnavailable(CloudApiError):
    """Cloud could not be reached or returned a transient failure."""


class CloudAuthExpired(CloudApiError):
    """Cloud rejected the stored account token."""


class CloudConflict(CloudSyncConflict, CloudApiError):
    """A cloud head changed while uploading an event batch."""

    def __init__(self, message: str, *, status_code: int | None = 409, retryable: bool = False) -> None:
        CloudApiError.__init__(self, message, status_code=status_code, retryable=retryable)


@dataclass(frozen=True)
class CloudSession:
    identity: UserIdentity
    access_token: str
    expires_in: int


def _identity(value: Any) -> UserIdentity:
    if not isinstance(value, Mapping):
        raise CloudApiError("Cloud response has no user identity.")
    user_id = str(value.get("id") or "")
    if not user_id:
        raise CloudApiError("Cloud response has an empty user identity.")
    return UserIdentity(
        user_id, str(value["email"]) if value.get("email") else None, str(value.get("kind") or "account")
    )


class CloudClient:
    """Authenticated cloud API client used by the local backend."""

    def __init__(
        self,
        base_url: str,
        *,
        token: str | None = None,
        timeout: float = 15.0,
        session=None,
        on_auth_expired: Callable[[], None] | None = None,
    ) -> None:
        normalized = str(base_url or "").strip().rstrip("/")
        if not normalized:
            raise ValueError("Cloud URL must not be empty.")
        parsed = urlsplit(normalized)
        local_http_hosts = {"localhost", "127.0.0.1", "::1"}
        if not parsed.netloc or not parsed.hostname:
            raise ValueError("Cloud URL must include a host.")
        if parsed.scheme == "https":
            pass
        elif parsed.scheme == "http" and parsed.hostname in local_http_hosts:
            pass
        else:
            raise ValueError("Cloud URL must use HTTPS except for local development hosts.")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Cloud URL must not contain credentials, query parameters, or fragments.")
        if timeout <= 0:
            raise ValueError("Cloud request timeout must be greater than zero.")
        self.base_url = normalized
        self.token = token or ""
        self.timeout = timeout
        self._session = session or requests.Session()
        self._on_auth_expired = on_auth_expired

    def with_token(
        self,
        token: str | None,
        *,
        on_auth_expired: Callable[[], None] | None = None,
    ) -> CloudClient:
        return CloudClient(
            self.base_url,
            token=token,
            timeout=self.timeout,
            session=self._session,
            on_auth_expired=on_auth_expired or self._on_auth_expired,
        )

    def close(self) -> None:
        close = getattr(self._session, "close", None)
        if callable(close):
            close()

    def _request(self, method: str, path: str, *, json: Any = None, expected: set[int] | None = None) -> Any:
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        try:
            response = self._session.request(
                method,
                f"{self.base_url}{path}",
                json=json,
                headers=headers,
                timeout=self.timeout,
                allow_redirects=False,
                verify=True,
            )
        except requests.RequestException as exc:
            raise CloudUnavailable("云端暂时不可用，请检查网络连接。", retryable=True) from exc
        response_content = getattr(response, "content", None)
        if expected and response.status_code in expected:
            try:
                return self._json_or_empty(response, response_content)
            except ValueError as exc:
                raise CloudApiError("云端返回了无效响应。", status_code=response.status_code) from exc
        if 300 <= response.status_code < 400:
            raise CloudApiError("云端不允许重定向。", status_code=response.status_code)
        if response.status_code == 401:
            if self._on_auth_expired is not None:
                try:
                    self._on_auth_expired()
                except Exception:
                    pass
            raise CloudAuthExpired("云端登录状态已过期，请重新登录。", status_code=401)
        if response.status_code == 409:
            detail = self._detail(response)
            raise CloudConflict(detail, status_code=409)
        if response.status_code in {408, 425, 429} or response.status_code >= 500:
            raise CloudUnavailable(self._detail(response), status_code=response.status_code, retryable=True)
        if response.status_code >= 400:
            raise CloudApiError(self._detail(response), status_code=response.status_code)
        try:
            return self._json_or_empty(response, response_content)
        except ValueError as exc:
            raise CloudApiError("云端返回了无效响应。", status_code=response.status_code) from exc

    @staticmethod
    def _json_or_empty(response: Any, content: Any) -> Any:
        if content is not None and not content:
            return {}
        try:
            return response.json()
        except AttributeError:
            if content is None:
                return {}
            raise
        except (TypeError, ValueError):
            if content is None:
                return {}
            raise ValueError("Cloud response is not valid JSON.")

    @staticmethod
    def _detail(response: Any) -> str:
        try:
            payload = response.json()
        except (TypeError, ValueError):
            payload = None
        if isinstance(payload, Mapping) and payload.get("detail"):
            return str(payload["detail"])
        return f"云端请求失败（HTTP {getattr(response, 'status_code', 'unknown')}）。"

    @staticmethod
    def _parse_session(payload: Mapping[str, Any]) -> CloudSession:
        if not isinstance(payload, Mapping):
            raise CloudApiError("Cloud response has an invalid session payload.")
        user = _identity(payload.get("user"))
        token = str(payload.get("access_token") or "")
        if not token:
            raise CloudApiError("Cloud response has no access token.")
        try:
            expires_in = int(payload.get("expires_in") or 0)
        except (TypeError, ValueError) as exc:
            raise CloudApiError("Cloud response has an invalid session expiry.") from exc
        if expires_in <= 0:
            raise CloudApiError("Cloud response has an invalid session expiry.")
        return CloudSession(user, token, expires_in)

    def register_code(self, email: str) -> None:
        self._request("POST", "/v1/auth/register/code", json={"email": email}, expected={202})

    def register(self, email: str, code: str, password: str) -> CloudSession:
        return self._parse_session(
            self._request("POST", "/v1/auth/register", json={"email": email, "code": code, "password": password})
        )

    def login(self, email: str, password: str) -> CloudSession:
        return self._parse_session(self._request("POST", "/v1/auth/login", json={"email": email, "password": password}))

    def reset_code(self, email: str) -> None:
        self._request("POST", "/v1/auth/password-reset/code", json={"email": email}, expected={202})

    def reset_password(self, email: str, code: str, password: str) -> CloudSession:
        return self._parse_session(
            self._request(
                "POST",
                "/v1/auth/password-reset/confirm",
                json={"email": email, "code": code, "password": password},
            )
        )

    def me(self) -> UserIdentity:
        payload = self._request("GET", "/v1/auth/me")
        if not isinstance(payload, Mapping):
            raise CloudApiError("Cloud response has an invalid identity payload.")
        return _identity(payload.get("user"))

    def logout(self) -> None:
        self._request("POST", "/v1/auth/logout", expected={200})

    def device_start(self, server_url: str = "") -> dict[str, Any]:
        return dict(self._request("POST", "/v1/devices/start", json={"server_url": server_url}))

    def device_info(self, grant: str) -> dict[str, Any]:
        query = urlencode({"grant": grant})
        return dict(self._request("GET", f"/v1/devices/info?{query}"))

    def device_approve(self, grant: str, approved: bool) -> dict[str, Any]:
        return dict(self._request("POST", "/v1/devices/approve", json={"grant": grant, "approved": approved}))

    def device_token(self, poll_secret: str) -> dict[str, Any]:
        return dict(
            self._request(
                "POST", "/v1/devices/token", json={"poll_secret": poll_secret}, expected={200, 202, 400, 403, 410}
            )
        )

    def ensure_user_key(self, key: bytes) -> None:
        if len(key) != 32:
            raise ValueError("The user data key must contain exactly 32 bytes.")
        self._request("POST", "/v1/sync/keys", json={"dek": base64.urlsafe_b64encode(key).decode("ascii")})

    def recover_user_key(self) -> bytes | None:
        payload = self._request("GET", "/v1/sync/keys")
        encoded = payload.get("dek") if isinstance(payload, Mapping) else None
        if not encoded:
            return None
        try:
            key = base64.b64decode(str(encoded).encode("ascii"), altchars=b"-_", validate=True)
        except (UnicodeError, ValueError) as exc:
            raise CloudApiError("云端返回了无效的数据密钥。") from exc
        if len(key) != 32:
            raise CloudApiError("云端返回了无效的数据密钥长度。")
        return key

    def push_events(
        self,
        *,
        session_id: str,
        parent_revision: int,
        device_id: str,
        event_id: str,
        envelope: Mapping[str, object],
        checksum: str,
        event_ids: list[str] | None = None,
    ) -> dict[str, object]:
        """Push one encrypted JSON event batch to the cloud head."""

        payload = self._request(
            "POST",
            "/v1/sync/push",
            json={
                "session_id": session_id,
                "parent_revision": parent_revision,
                "device_id": device_id,
                "event_id": event_id,
                "event_ids": list(event_ids or []),
                "envelope": dict(envelope),
                "checksum": checksum,
            },
        )
        if not isinstance(payload, Mapping):
            raise CloudApiError("云端返回了无效的事件确认。")
        return dict(payload)

    def pull_events(self, *, session_id: str, after_revision: int) -> dict[str, object]:
        """Pull encrypted JSON events after a local revision."""

        query = urlencode({"session_id": session_id, "after_revision": after_revision})
        payload = self._request("GET", f"/v1/sync/pull?{query}")
        if not isinstance(payload, Mapping):
            raise CloudApiError("云端返回了无效的事件批次。")
        return dict(payload)

    def list_sync_heads(self) -> list[dict[str, object]]:
        payload = self._request("GET", "/v1/sync/heads")
        if not isinstance(payload, Mapping) or not isinstance(payload.get("heads"), list):
            raise CloudApiError("云端返回了无效的同步 head 列表。")
        return [dict(item) for item in payload["heads"] if isinstance(item, Mapping)]


__all__ = ["CloudApiError", "CloudAuthExpired", "CloudClient", "CloudConflict", "CloudSession", "CloudUnavailable"]
