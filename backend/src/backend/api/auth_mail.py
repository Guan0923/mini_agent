"""Email delivery ports and the generic SMTP implementation."""

from __future__ import annotations

import smtplib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Protocol


class MailDeliveryError(RuntimeError):
    """The configured mail transport could not deliver a message."""


class Mailer(Protocol):
    def send_code(self, recipient: str, code: str, purpose: str) -> None: ...


@dataclass(frozen=True)
class SMTPSettings:
    host: str
    port: int
    username: str
    password: str
    from_address: str
    starttls: bool = True

    @classmethod
    def from_config(cls, values: dict[str, object]) -> SMTPSettings | None:
        host = values.get("smtp_host")
        sender = values.get("from_address")
        if not isinstance(host, str) or not host.strip() or not isinstance(sender, str) or not sender.strip():
            return None
        port = values.get("smtp_port", 587)
        try:
            parsed_port = int(port)
        except (TypeError, ValueError):
            parsed_port = 587
        username = values.get("smtp_username", "")
        password = values.get("smtp_password", "")
        return cls(
            host.strip(),
            parsed_port,
            str(username),
            str(password),
            sender.strip(),
            bool(values.get("use_starttls", True)),
        )


class SMTPMailer:
    """Blocking stdlib SMTP wrapped behind a tiny injectable port."""

    def __init__(self, settings: SMTPSettings) -> None:
        self.settings = settings
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="mini-agent-mail")

    def send_code(self, recipient: str, code: str, purpose: str) -> None:
        future = self._executor.submit(self._send, recipient, code, purpose)
        try:
            future.result(timeout=30)
        except Exception as exc:
            raise MailDeliveryError("验证码邮件发送失败，请稍后再试。") from exc

    def close(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)

    def _send(self, recipient: str, code: str, purpose: str) -> None:
        label = "注册" if purpose == "register" else "重置密码"
        message = EmailMessage()
        message["Subject"] = f"Mini-Agent 邮箱验证码（{label}）"
        message["From"] = self.settings.from_address
        message["To"] = recipient
        message.set_content(
            f"您好！\n\n您的 Mini-Agent {label}验证码是：{code}\n"
            "验证码 10 分钟内有效，且只能使用一次。\n"
            "如果这不是您的操作，请忽略此邮件。\n"
        )
        with smtplib.SMTP(self.settings.host, self.settings.port, timeout=20) as server:
            if self.settings.starttls:
                server.starttls()
            if self.settings.username:
                server.login(self.settings.username, self.settings.password)
            server.send_message(message)


class NullMailer:
    """Explicit development fallback that refuses to pretend delivery worked."""

    def send_code(self, recipient: str, code: str, purpose: str) -> None:
        raise MailDeliveryError("服务端尚未配置邮箱发送功能。")
