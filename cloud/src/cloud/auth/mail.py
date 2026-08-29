"""SMTP delivery owned by the cloud service."""

from __future__ import annotations

import os
import smtplib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from email.message import EmailMessage


class MailDeliveryError(RuntimeError):
    """The configured SMTP transport could not deliver a code."""


@dataclass(frozen=True)
class SMTPSettings:
    host: str
    port: int
    username: str
    password: str
    from_address: str
    starttls: bool = True

    @classmethod
    def from_environment(cls) -> SMTPSettings | None:
        host = os.environ.get("MINI_AGENT_SMTP_HOST", "").strip()
        sender = os.environ.get("MINI_AGENT_SMTP_FROM", "").strip()
        if not host or not sender:
            return None
        try:
            port = int(os.environ.get("MINI_AGENT_SMTP_PORT", "587"))
        except ValueError:
            port = 587
        return cls(
            host,
            port,
            os.environ.get("MINI_AGENT_SMTP_USERNAME", ""),
            os.environ.get("MINI_AGENT_SMTP_PASSWORD", ""),
            sender,
            os.environ.get("MINI_AGENT_SMTP_STARTTLS", "true").lower() not in {"0", "false", "no"},
        )


class SMTPMailer:
    def __init__(self, settings: SMTPSettings) -> None:
        self.settings = settings
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="mini-agent-cloud-mail")

    def send_code(self, recipient: str, code: str, purpose: str) -> None:
        future = self._executor.submit(self._send, recipient, code, purpose)
        try:
            future.result(timeout=30)
        except Exception as exc:
            raise MailDeliveryError("验证码邮件发送失败，请稍后再试。") from exc

    def _send(self, recipient: str, code: str, purpose: str) -> None:
        label = "注册" if purpose == "register" else "重置密码"
        message = EmailMessage()
        message["Subject"] = f"Mini-Agent 邮箱验证码（{label}）"
        message["From"] = self.settings.from_address
        message["To"] = recipient
        message.set_content(f"您好！\n\n您的 Mini-Agent {label}验证码是：{code}\n验证码 10 分钟内有效。\n")
        with smtplib.SMTP(self.settings.host, self.settings.port, timeout=20) as server:
            if self.settings.starttls:
                server.starttls()
            if self.settings.username:
                server.login(self.settings.username, self.settings.password)
            server.send_message(message)

    def close(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)


class NullMailer:
    def send_code(self, recipient: str, code: str, purpose: str) -> None:
        raise MailDeliveryError("服务端尚未配置邮箱发送功能。")


__all__ = ["MailDeliveryError", "NullMailer", "SMTPMailer", "SMTPSettings"]
