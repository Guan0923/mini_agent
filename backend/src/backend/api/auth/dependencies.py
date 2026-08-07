"""FastAPI dependencies for browser and device authentication."""

from __future__ import annotations

from fastapi import HTTPException, Request, status

from .types import UserIdentity


def current_identity(request: Request) -> tuple[UserIdentity, str, str] | None:
    service = request.app.state.web.auth_service
    resolved = service.identity_from_request(request)
    if resolved is not None:
        identity, auth_kind, token = resolved
        if (
            auth_kind == "browser"
            and request.method not in {"GET", "HEAD", "OPTIONS"}
            and not service.origin_allowed(request)
        ):
            raise HTTPException(status_code=403, detail="不允许的请求来源。")
        request.state.user = identity
        request.state.auth_kind = auth_kind
        request.state.auth_token = token
    return resolved


def require_user(request: Request) -> UserIdentity:
    resolved = current_identity(request)
    if resolved is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="请先登录。", headers={"WWW-Authenticate": "Bearer"}
        )
    return resolved[0]


def require_browser_user(request: Request) -> UserIdentity:
    resolved = current_identity(request)
    if resolved is None or resolved[1] != "browser":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="请在浏览器中登录。")
    return resolved[0]
