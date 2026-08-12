"""Local mail port compatibility.

Verification mail is sent by the cloud service.  The local backend keeps a
small error type and an explicit no-op adapter for injected/offline tests,
but never owns SMTP configuration or connections.
"""

from __future__ import annotations

from typing import Protocol


class MailDeliveryError(RuntimeError):
    """The configured cloud mail operation could not be completed."""


class Mailer(Protocol):
    def send_code(self, recipient: str, code: str, purpose: str) -> None: ...


class NullMailer:
    """Explicit fallback used only when no cloud account service is configured."""

    def send_code(self, recipient: str, code: str, purpose: str) -> None:
        del recipient, code, purpose
        raise MailDeliveryError("云端账户服务尚未配置，无法发送验证码。")


__all__ = ["MailDeliveryError", "Mailer", "NullMailer"]
