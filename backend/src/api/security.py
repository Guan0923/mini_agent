"""Loopback browser-origin policy independent of user authentication."""

from __future__ import annotations

import os
from dataclasses import dataclass
from ipaddress import ip_address
from urllib.parse import urlsplit

from fastapi import Request


@dataclass(frozen=True)
class LocalWebSettings:
    allowed_origins: tuple[str, ...] = ("http://localhost:5173", "http://127.0.0.1:5173")

    @classmethod
    def from_env(cls) -> LocalWebSettings:
        raw = os.environ.get("MINI_AGENT_ALLOWED_ORIGINS", "")
        origins = tuple(item.strip().rstrip("/") for item in raw.split(",") if item.strip())
        resolved = origins or cls.allowed_origins
        invalid = [origin for origin in resolved if not _is_loopback_origin(origin)]
        if invalid:
            raise ValueError("MINI_AGENT_ALLOWED_ORIGINS accepts only absolute HTTP(S) loopback origins")
        return cls(allowed_origins=resolved)


def _is_loopback_origin(origin: str) -> bool:
    try:
        parsed = urlsplit(origin)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            return False
        if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            return False
        parsed.port
    except ValueError:
        return False
    if parsed.hostname.casefold() == "localhost":
        return True
    try:
        return ip_address(parsed.hostname).is_loopback
    except ValueError:
        return False


def origin_allowed(request: Request, settings: LocalWebSettings) -> bool:
    return browser_origin_allowed(request.headers.get("origin"), settings)


def browser_origin_allowed(origin: str | None, settings: LocalWebSettings) -> bool:
    return origin is None or origin.rstrip("/") in settings.allowed_origins


__all__ = ["LocalWebSettings", "browser_origin_allowed", "origin_allowed"]
