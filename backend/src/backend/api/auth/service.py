"""Authentication policy, validation and session-cookie helpers."""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import TYPE_CHECKING

from email_validator import EmailNotValidError, validate_email
from fastapi import Request, Response

from backend.configuration import load_config, section
from backend.storage.auth.types import AuthRepository

from .mail import MailDeliveryError
from .types import AuthError, RateLimitError, UserIdentity

if TYPE_CHECKING:
    from ..state import WebAppState


COOKIE_NAME = "mini_agent_session"
SESSION_TTL_SECONDS = 2_592_000
CODE_TTL_SECONDS = 600


@dataclass(frozen=True)
class WebAuthSettings:
    public_url: str = "http://localhost:5173"
    allowed_origins: tuple[str, ...] = ("http://localhost:5173", "http://127.0.0.1:5173")
    cookie_secure: bool = False

    @classmethod
    def from_state(cls, state: WebAppState) -> WebAuthSettings:
        try:
            values = dict(section(load_config(state.config_path), "web"))
        except Exception:
            values = {}
        origins = values.get("allowed_origins", cls.allowed_origins)
        if isinstance(origins, str):
            parsed_origins = (origins.rstrip("/"),)
        elif isinstance(origins, list | tuple):
            parsed_origins = tuple(str(item).rstrip("/") for item in origins if str(item))
        else:
            parsed_origins = cls.allowed_origins
        cookie_secure = values.get("cookie_secure", False)
        if isinstance(cookie_secure, str):
            cookie_secure = cookie_secure.strip().lower() in {"1", "true", "yes", "on"}
        return cls(
            public_url=str(values.get("public_url", cls.public_url)).rstrip("/"),
            allowed_origins=parsed_origins or cls.allowed_origins,
            cookie_secure=bool(cookie_secure),
        )


def normalize_email(value: str) -> str:
    try:
        result = validate_email(value.strip(), check_deliverability=False)
    except EmailNotValidError as exc:
        raise AuthError("请输入有效的邮箱地址。") from exc
    return result.normalized.lower()


def validate_password(password: str) -> str:
    if not 12 <= len(password) <= 128:
        raise AuthError("密码长度需要在 12 到 128 个字符之间。")
    return password


class AuthService:
    def __init__(self, state: WebAppState) -> None:
        self.state = state
        self.store: AuthRepository = state.auth
        self.mailer = state.mailer
        self.settings = WebAuthSettings.from_state(state)
        self._dummy_hash = self.store.password_hash("mini-agent-invalid-password")

    def request_code(self, email_value: str, purpose: str, ip_address: str | None) -> None:
        email = normalize_email(email_value)
        allowed, retry_after = self.store.can_send(email, purpose, ip_address)
        if not allowed:
            raise RateLimitError(f"请求过于频繁，请在 {retry_after} 秒后再试。", retry_after)
        if purpose == "register" and self.store.user_by_email(email) is not None:
            return
        if purpose == "reset" and self.store.user_by_email(email) is None:
            return
        code = f"{secrets.randbelow(1_000_000):06d}"
        try:
            self.mailer.send_code(email, code, purpose)
        except MailDeliveryError:
            raise
        self.store.insert_challenge(email, purpose, code, ip_address, ttl_seconds=CODE_TTL_SECONDS)

    def register(self, email_value: str, code: str, password: str) -> UserIdentity:
        email = normalize_email(email_value)
        validate_password(password)
        if len(code) != 6 or not code.isdigit():
            raise AuthError("验证码无效或已过期。")
        try:
            return self.store.register_user(email, code, password)
        except ValueError as exc:
            raise AuthError(str(exc)) from exc

    def login(self, email_value: str, password: str, ip_address: str | None) -> UserIdentity:
        email = normalize_email(email_value)
        email_allowed = self.store.consume_limit(f"email:{email}", "login:15m", 10, 900)
        ip_allowed = self.store.consume_limit(f"ip:{ip_address}", "login:15m", 10, 900) if ip_address else True
        if not email_allowed or not ip_allowed:
            raise RateLimitError()
        identity = self.store.authenticate(email, password)
        if identity is None:
            self.store.verify_password(password, self._dummy_hash)
            raise AuthError("邮箱或密码错误。")
        return identity

    def reset_password(self, email_value: str, code: str, password: str) -> UserIdentity:
        email = normalize_email(email_value)
        validate_password(password)
        if len(code) != 6 or not code.isdigit():
            raise AuthError("验证码无效或已过期。")
        try:
            return self.store.reset_password(email, code, password)
        except ValueError as exc:
            raise AuthError(str(exc)) from exc

    def browser_session(self, identity: UserIdentity) -> str:
        return self.store.create_session(identity.id, "browser", ttl_seconds=SESSION_TTL_SECONDS)

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

    def origin_allowed(self, request: Request) -> bool:
        origin = request.headers.get("origin")
        return origin is None or origin in self.settings.allowed_origins

    def start_device(self, server_url: str) -> tuple[str, str, int]:
        return self.store.start_device(server_url)

    def device_url(self, browser_secret: str) -> str:
        return f"{self.settings.public_url}/device/approve?grant={browser_secret}"
