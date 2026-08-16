"""Authentication policy, validation and session-cookie helpers."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from fastapi import Request, Response

from backend.cloud import CloudApiError, CloudAuthExpired, CloudClient, CloudSession, CloudUnavailable
from backend.storage.auth.types import AuthRepository, AuthStorageUnavailable

from .types import AuthError, UserIdentity

if TYPE_CHECKING:
    from ..state import WebAppState


COOKIE_NAME = "mini_agent_session"
SESSION_TTL_SECONDS = 2_592_000


@dataclass(frozen=True)
class WebAuthSettings:
    public_url: str = "http://localhost:5173"
    allowed_origins: tuple[str, ...] = ("http://localhost:5173", "http://127.0.0.1:5173")
    cookie_secure: bool = False

    @classmethod
    def from_state(cls, state: WebAppState) -> WebAuthSettings:
        del state
        raw_origins = os.environ.get("MINI_AGENT_ALLOWED_ORIGINS", "")
        parsed_origins = (
            tuple(item.strip().rstrip("/") for item in raw_origins.split(",") if item.strip()) or cls.allowed_origins
        )
        cookie_secure = os.environ.get("MINI_AGENT_COOKIE_SECURE", "false").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        return cls(
            public_url=os.environ.get("MINI_AGENT_PUBLIC_URL", cls.public_url).rstrip("/"),
            allowed_origins=parsed_origins,
            cookie_secure=bool(cookie_secure),
        )


class AuthService:
    def __init__(self, state: WebAppState) -> None:
        self.state = state
        self.store: AuthRepository = state.auth
        self.cloud: CloudClient | None = getattr(state, "cloud_client", None)
        # ``set_cloud_token`` creates ``user.db``.  Keep track of accounts
        # whose local tree did not exist before that write so the API layer
        # can still trigger the first-login cloud restore.  Without this
        # marker, the token row itself would look like an existing local
        # account and silently suppress automatic recovery on new devices.
        self._first_cloud_login: set[str] = set()
        self.settings = WebAuthSettings.from_state(state)

    def request_code(self, email_value: str, purpose: str, ip_address: str | None) -> None:
        del ip_address
        cloud = self._require_cloud()
        if purpose == "register":
            cloud.register_code(email_value)
        elif purpose == "reset":
            cloud.reset_code(email_value)
        else:
            raise AuthError("不支持的验证码类型。")

    def register(self, email_value: str, code: str, password: str) -> UserIdentity:
        session = self._require_cloud().register(email_value, code, password)
        self._remember_cloud_session(session)
        return session.identity

    def login(self, email_value: str, password: str, ip_address: str | None) -> UserIdentity:
        del ip_address
        session = self._require_cloud().login(email_value, password)
        self._remember_cloud_session(session)
        return session.identity

    def reset_password(self, email_value: str, code: str, password: str) -> UserIdentity:
        session = self._require_cloud().reset_password(email_value, code, password)
        self._remember_cloud_session(session)
        return session.identity

    def browser_session(self, identity: UserIdentity) -> str:
        if self.cloud is not None:
            self.store.upsert_identity(identity)
        return self.store.create_session(identity.id, "browser", ttl_seconds=SESSION_TTL_SECONDS)

    def get_or_create_guest(self) -> tuple[UserIdentity, bool]:
        """Return the device-scoped guest identity and whether it was new."""

        getter = getattr(self.store, "get_or_create_guest", None)
        if not callable(getter):
            raise AuthStorageUnavailable("认证存储不支持设备级游客身份。")
        identity, created = getter()
        if not identity.is_guest:
            raise AuthStorageUnavailable("认证存储返回了无效的游客身份。")
        return identity, bool(created)

    def guest_session(self, identity: UserIdentity | None = None) -> tuple[UserIdentity, str]:
        if identity is None:
            identity, _created = self.get_or_create_guest()
        if self.cloud is not None:
            self.store.upsert_identity(identity)
            return identity, self.store.create_session(identity.id, "browser", ttl_seconds=SESSION_TTL_SECONDS)
        create_atomic = getattr(self.store, "create_guest_session", None)
        if not callable(create_atomic):
            raise AuthStorageUnavailable("认证存储不支持原子游客会话创建。")
        token = create_atomic(identity.id, ttl_seconds=SESSION_TTL_SECONDS)
        return identity, token

    def set_browser_cookie(self, response: Response, token: str) -> None:
        response.set_cookie(
            COOKIE_NAME,
            token,
            max_age=SESSION_TTL_SECONDS,
            httponly=True,
            secure=self.settings.cookie_secure,
            samesite="lax",
            path="/",
        )

    def clear_browser_cookie(self, response: Response) -> None:
        response.delete_cookie(COOKIE_NAME, path="/")

    def identity_from_request(self, request: Request) -> tuple[UserIdentity, str, str] | None:
        header = request.headers.get("authorization", "")
        token = None
        kind = "browser"
        if header.lower().startswith("bearer "):
            token = header[7:].strip()
            kind = "device"
        if not token:
            token = request.cookies.get(COOKIE_NAME)
        if not token:
            return None
        resolved = self.store.resolve_token(token)
        if resolved is None:
            return None
        identity, stored_kind = resolved
        if stored_kind != kind:
            return None
        return identity, kind, token

    def device_info(self, grant: str) -> dict[str, object] | None:
        try:
            return self._require_cloud().device_info(grant)
        except (CloudUnavailable, CloudAuthExpired):
            raise
        except CloudApiError:
            return None

    def approve_device(self, grant: str, user_id: str, approved: bool) -> bool:
        try:
            self._cloud_for_user(user_id).device_approve(grant, approved)
            return True
        except (CloudUnavailable, CloudAuthExpired):
            raise
        except CloudApiError:
            return False

    def poll_device(self, poll_secret: str) -> tuple[str, str | None]:
        cloud = self._require_cloud()
        payload = cloud.device_token(poll_secret)
        status_name = str(payload.get("status") or "invalid_grant")
        cloud_token = str(payload.get("access_token") or "")
        if status_name != "approved" or not cloud_token:
            return status_name, None
        # The cloud token is a remote credential, not the token accepted by
        # the loopback API. Validate it once, retain it encrypted in the
        # account's user.db, and return a local device session whose client.db
        # row stores only a hash. This preserves the local/cloud boundary.
        identity = cloud.with_token(cloud_token).me()
        self._remember_cloud_session(
            CloudSession(identity, cloud_token, int(payload.get("expires_in") or SESSION_TTL_SECONDS))
        )
        local_token = self.store.create_session(identity.id, "device", ttl_seconds=SESSION_TTL_SECONDS)
        return "approved", local_token

    def revoke_token(self, identity: UserIdentity, local_token: str) -> None:
        self.store.revoke_token(local_token)
        if self.cloud is not None:
            try:
                self._cloud_for_user(identity.id).logout()
            except CloudAuthExpired:
                pass
            except CloudUnavailable:
                # Local logout is authoritative on this device even offline.
                pass
            finally:
                # Logout is an explicit local user action.  Do not leave a
                # reusable remote bearer credential in ``user.db`` merely
                # because the cloud endpoint was unreachable or the token had
                # already expired.  Network failures preserve an ordinary
                # cached session during background work, but they must not
                # preserve credentials after the user asks to sign out.
                clearer = getattr(self.state.settings, "clear_cloud_token", None)
                if callable(clearer):
                    clearer(identity.id)

    def _remember_cloud_session(self, session) -> None:
        user_root = getattr(self.state, "data_root", None)
        if user_root is not None:
            try:
                from ..user_data import user_root as resolve_user_root

                if not (resolve_user_root(user_root, session.identity.id) / "user.db").is_file():
                    self._first_cloud_login.add(session.identity.id)
            except (OSError, ValueError):
                # The normal local-data preparation path reports a precise
                # error.  Authentication should not fail merely because the
                # best-effort preflight could not inspect a path.
                pass
        self.store.upsert_identity(session.identity)
        settings = getattr(self.state, "settings", None)
        setter = getattr(settings, "set_cloud_token", None)
        if callable(setter):
            setter(session.identity.id, session.access_token, time.time() + max(0, session.expires_in))

    def consume_first_cloud_login(self, user_id: str) -> bool:
        """Return and clear the first-login restore marker for one account."""

        if user_id in self._first_cloud_login:
            self._first_cloud_login.remove(user_id)
            return True
        return False

    def _cloud_token(self, user_id: str) -> str:
        settings = getattr(self.state, "settings", None)
        reader = getattr(settings, "cloud_token_for_user", None)
        value = reader(user_id) if callable(reader) else None
        if not isinstance(value, dict) or not value.get("token"):
            raise CloudAuthExpired("云端登录状态已过期，请重新登录。", status_code=401)
        expires_at = float(value.get("expires_at") or 0)
        if expires_at and expires_at <= time.time():
            clearer = getattr(settings, "clear_cloud_token", None)
            if callable(clearer):
                clearer(user_id)
            raise CloudAuthExpired("云端登录状态已过期，请重新登录。", status_code=401)
        return str(value["token"])

    def _cloud_for_user(self, user_id: str) -> CloudClient:
        return self._require_cloud().with_token(
            self._cloud_token(user_id),
            on_auth_expired=lambda: getattr(self.state.settings, "clear_cloud_token", lambda _id: None)(user_id),
        )

    def origin_allowed(self, request: Request) -> bool:
        origin = request.headers.get("origin")
        return origin is None or origin in self.settings.allowed_origins

    def start_device(self, server_url: str) -> tuple[str, str, int]:
        payload = self._require_cloud().device_start(server_url)
        return str(payload["poll_secret"]), str(payload["verification_url"]), int(payload.get("expires_in") or 600)

    def _require_cloud(self) -> CloudClient:
        if self.cloud is None:
            raise CloudUnavailable("云端账户服务暂不可用，请连接网络后重试。", retryable=True)
        return self.cloud

    def device_url(self, browser_secret: str) -> str:
        if browser_secret.startswith(("http://", "https://")):
            return browser_secret
        return f"{self.settings.public_url}/device/approve?grant={browser_secret}"
