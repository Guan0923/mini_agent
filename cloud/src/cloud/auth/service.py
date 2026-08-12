"""Cloud authentication policy, independent from local client state."""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from uuid import uuid4

from email_validator import EmailNotValidError, validate_email

from .mail import MailDeliveryError
from .types import AuthError, RateLimitError, UserIdentity

SESSION_TTL_SECONDS = 2_592_000
CODE_TTL_SECONDS = 600


@dataclass(frozen=True)
class CloudAuthSettings:
    public_url: str = "http://localhost:5173"

    @classmethod
    def from_environment(cls) -> CloudAuthSettings:
        return cls(public_url=os.environ.get("MINI_AGENT_CLOUD_PUBLIC_URL", cls.public_url).rstrip("/"))


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


class CloudAuthService:
    def __init__(self, repository, mailer) -> None:
        self.store = repository
        self.mailer = mailer
        self.settings = CloudAuthSettings.from_environment()
        password_hash = getattr(self.store, "password_hash", None)
        self._dummy_hash = password_hash("mini-agent-invalid-password") if callable(password_hash) else ""

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

    def register(self, email_value: str, code: str, password: str) -> tuple[UserIdentity, str]:
        email = normalize_email(email_value)
        validate_password(password)
        if len(code) != 6 or not code.isdigit():
            raise AuthError("验证码无效或已过期。")
        try:
            identity = self.store.register_user(email, code, password)
        except ValueError as exc:
            raise AuthError(str(exc)) from exc
        return identity, self.create_session(identity)

    def login(self, email_value: str, password: str, ip_address: str | None) -> tuple[UserIdentity, str]:
        email = normalize_email(email_value)
        email_allowed = self.store.consume_limit(f"email:{email}", "login:15m", 10, 900)
        ip_allowed = self.store.consume_limit(f"ip:{ip_address}", "login:15m", 10, 900) if ip_address else True
        if not email_allowed or not ip_allowed:
            raise RateLimitError()
        identity = self.store.authenticate(email, password)
        if identity is None:
            self.store.verify_password(password, self._dummy_hash)
            raise AuthError("邮箱或密码错误。")
        return identity, self.create_session(identity)

    def reset_password(self, email_value: str, code: str, password: str) -> tuple[UserIdentity, str]:
        email = normalize_email(email_value)
        validate_password(password)
        if len(code) != 6 or not code.isdigit():
            raise AuthError("验证码无效或已过期。")
        try:
            identity = self.store.reset_password(email, code, password)
        except ValueError as exc:
            raise AuthError(str(exc)) from exc
        return identity, self.create_session(identity)

    def create_session(self, identity: UserIdentity, kind: str = "device") -> str:
        return self.store.create_session(identity.id, kind, ttl_seconds=SESSION_TTL_SECONDS)

    def resolve_token(self, token: str) -> tuple[UserIdentity, str] | None:
        return self.store.resolve_token(token)

    def start_device(self, server_url: str) -> tuple[str, str, int]:
        return self.store.start_device(server_url)

    def device_url(self, browser_secret: str) -> str:
        return f"{self.settings.public_url}/device/approve?grant={browser_secret}"

    def new_guest_identity(self) -> UserIdentity:
        # Kept for wire compatibility; guests are normally created locally.
        return UserIdentity(str(uuid4()), None, "guest")


__all__ = ["CloudAuthService", "CloudAuthSettings", "CODE_TTL_SECONDS", "SESSION_TTL_SECONDS", "normalize_email"]
