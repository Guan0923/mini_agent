"""Public browser authentication and terminal device-authorization routes."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .auth_dependencies import require_browser_user, require_user
from .auth_mail import MailDeliveryError
from .auth_types import AuthError, RateLimitError, UserIdentity
from .user_data import migrate_legacy_for_owner

router = APIRouter(prefix="/api/auth", tags=["auth"])


class EmailRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)


class RegisterRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    code: str = Field(min_length=6, max_length=6)
    password: str = Field(min_length=1, max_length=128)


class LoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=128)


class PasswordResetRequest(RegisterRequest):
    pass


class DeviceStartResponse(BaseModel):
    poll_secret: str
    verification_url: str
    expires_in: int
    poll_interval: int = 2


class DevicePollRequest(BaseModel):
    poll_secret: str = Field(min_length=20, max_length=256)


class DeviceApproveRequest(BaseModel):
    grant: str = Field(min_length=20, max_length=256)
    approved: bool


def _error(exc: Exception) -> HTTPException:
    if isinstance(exc, RateLimitError):
        return HTTPException(status_code=429, detail=str(exc), headers={"Retry-After": str(exc.retry_after)})
    if isinstance(exc, MailDeliveryError):
        return HTTPException(status_code=503, detail=str(exc))
    if isinstance(exc, AuthError):
        return HTTPException(status_code=400, detail=str(exc))
    return HTTPException(status_code=400, detail="请求无法完成。")


def _origin_guard(request: Request) -> None:
    if not request.app.state.web.auth_service.origin_allowed(request):
        raise HTTPException(status_code=403, detail="不允许的请求来源。")


def _identity_payload(identity: UserIdentity) -> dict[str, object]:
    return {"id": identity.id, "email": identity.email, "legacy_owner": identity.legacy_owner}


def _prepare_user(request: Request, identity: UserIdentity) -> None:
    state = request.app.state.web
    state.user_paths(identity.id)
    if not identity.legacy_owner:
        return
    key = f"legacy_migration:{identity.id}"
    current = state.auth.metadata(key)
    if current == "complete":
        return
    try:
        migrate_legacy_for_owner(
            state.data_root,
            identity,
            state.paths,
            state.chat_workspace,
            status=current,
            set_status=lambda value: state.auth.set_metadata(key, value),
        )
    except OSError:
        # Account creation/login remains usable; the idempotent migration retries
        # on the next authenticated request.
        return


def _set_session(request: Request, response: Response, identity: UserIdentity) -> None:
    service = request.app.state.web.auth_service
    _prepare_user(request, identity)
    service.set_browser_cookie(response, service.browser_session(identity))


@router.post("/register/code", status_code=202)
def request_register_code(body: EmailRequest, request: Request) -> dict[str, str]:
    _origin_guard(request)
    try:
        request.app.state.web.auth_service.request_code(
            body.email, "register", request.client.host if request.client else None
        )
    except Exception as exc:
        raise _error(exc) from exc
    return {"detail": "如果邮箱可用，验证码已发送。"}


@router.post("/register")
def register(body: RegisterRequest, request: Request, response: Response) -> dict[str, object]:
    _origin_guard(request)
    service = request.app.state.web.auth_service
    try:
        identity = service.register(body.email, body.code, body.password)
    except Exception as exc:
        raise _error(exc) from exc
    _set_session(request, response, identity)
    return {"user": _identity_payload(identity)}


@router.post("/login")
def login(body: LoginRequest, request: Request, response: Response) -> dict[str, object]:
    _origin_guard(request)
    service = request.app.state.web.auth_service
    try:
        identity = service.login(body.email, body.password, request.client.host if request.client else None)
    except Exception as exc:
        raise _error(exc) from exc
    _set_session(request, response, identity)
    return {"user": _identity_payload(identity)}


@router.post("/password-reset/code", status_code=202)
def request_reset_code(body: EmailRequest, request: Request) -> dict[str, str]:
    _origin_guard(request)
    try:
        request.app.state.web.auth_service.request_code(
            body.email, "reset", request.client.host if request.client else None
        )
    except Exception as exc:
        raise _error(exc) from exc
    return {"detail": "如果邮箱已注册，验证码已发送。"}


@router.post("/password-reset/confirm")
def reset_password(body: PasswordResetRequest, request: Request, response: Response) -> dict[str, object]:
    _origin_guard(request)
    service = request.app.state.web.auth_service
    try:
        identity = service.reset_password(body.email, body.code, body.password)
    except Exception as exc:
        raise _error(exc) from exc
    _set_session(request, response, identity)
    return {"user": _identity_payload(identity)}


@router.get("/me")
def me(identity: Annotated[UserIdentity, Depends(require_user)], request: Request) -> dict[str, object]:
    _prepare_user(request, identity)
    return _identity_payload(identity)


@router.post("/logout")
def logout(
    request: Request, response: Response, identity: Annotated[UserIdentity, Depends(require_user)]
) -> dict[str, str]:
    _origin_guard(request)
    token = getattr(request.state, "auth_token", None)
    if isinstance(token, str):
        request.app.state.web.auth.revoke_token(token)
    request.app.state.web.auth_service.clear_browser_cookie(response)
    return {"detail": "已退出登录。"}


@router.post("/device/start", response_model=DeviceStartResponse)
def device_start(request: Request) -> DeviceStartResponse:
    _origin_guard(request)
    service = request.app.state.web.auth_service
    server_url = str(request.base_url).rstrip("/")
    poll, browser, expires = service.start_device(server_url)
    return DeviceStartResponse(
        poll_secret=poll,
        verification_url=service.device_url(browser),
        expires_in=expires,
    )


@router.get("/device/info")
def device_info(grant: str, request: Request) -> dict[str, object]:
    info = request.app.state.web.auth.device_info(grant)
    if info is None:
        raise HTTPException(status_code=404, detail="设备授权请求不存在或已过期。")
    return info


@router.post("/device/approve")
def device_approve(
    body: DeviceApproveRequest,
    request: Request,
    identity: Annotated[UserIdentity, Depends(require_browser_user)],
) -> dict[str, str]:
    _origin_guard(request)
    accepted = request.app.state.web.auth.approve_device(body.grant, identity.id, body.approved)
    if not accepted:
        raise HTTPException(status_code=410, detail="设备授权请求不存在、已处理或已过期。")
    return {"status": "approved" if body.approved else "denied"}


@router.post("/device/token")
def device_token(body: DevicePollRequest, request: Request) -> JSONResponse:
    status_name, token = request.app.state.web.auth.poll_device(body.poll_secret)
    if status_name == "pending":
        return JSONResponse({"status": "authorization_pending"}, status_code=202)
    if status_name == "approved" and token:
        return JSONResponse(
            {"status": "approved", "access_token": token, "token_type": "Bearer", "expires_in": 2_592_000}
        )
    if status_name == "denied":
        return JSONResponse({"status": "access_denied"}, status_code=403)
    if status_name == "expired":
        return JSONResponse({"status": "expired"}, status_code=410)
    return JSONResponse({"status": "invalid_grant"}, status_code=400)
