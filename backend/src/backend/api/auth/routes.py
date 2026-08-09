"""Public browser authentication and terminal device-authorization routes."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, StrictBool, field_validator

from backend.domain import DEFAULT_TIME_ZONE, validate_time_zone
from backend.storage.auth.types import AuthStorageUnavailable

from ..user_data import migrate_legacy_for_owner
from .dependencies import require_browser_user, require_user
from .mail import MailDeliveryError
from .types import AuthError, RateLimitError, UserIdentity

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


class ProfilePayload(BaseModel):
    display_name: str = Field(default="", max_length=80)
    agent_preferences: str = Field(default="", max_length=4000)


class AgentConfigPayload(BaseModel):
    tone: str = Field(default="balanced", max_length=40)
    verbosity: str = Field(default="balanced", max_length=40)
    initiative: str = Field(default="balanced", max_length=40)
    custom_instructions: str = Field(default="", max_length=4000)
    # ``minimal`` is retained as the persisted value for the UI's
    # ``简洁`` label.  ``developer`` is accepted by the settings contract but
    # hidden by production builds on the client.
    display_mode: Literal["minimal", "medium", "verbose", "developer"] = "medium"
    timezone: str = Field(default=DEFAULT_TIME_ZONE, max_length=80)
    location_enabled: StrictBool = False

    @field_validator("timezone")
    @classmethod
    def supported_timezone(cls, value: str) -> str:
        return validate_time_zone(value)


class ProviderConfigPayload(BaseModel):
    provider: str = Field(default="deepseek", min_length=1, max_length=80)
    protocol: str = Field(default="chat_completions", min_length=1, max_length=40)
    base_url: str = Field(default="", max_length=2000)
    model: str = Field(default="", max_length=300)
    max_tokens: int = Field(default=8192, ge=1, le=384000)
    context_size: int = Field(default=1024000, ge=1)
    tokenizer_model: str = Field(default="deepseek-ai/DeepSeek-V3", max_length=300)
    api_key: str | None = Field(default=None, max_length=4096)


def _error(exc: Exception) -> HTTPException:
    if isinstance(exc, AuthStorageUnavailable):
        return HTTPException(status_code=503, detail="认证与用户设置服务暂不可用。")
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


def _identity_payload(request: Request, identity: UserIdentity) -> dict[str, object]:
    return {
        "id": identity.id,
        "email": identity.email,
        "legacy_owner": identity.legacy_owner,
        **request.app.state.web.settings.profile_for_user(identity.id),
    }


def _prepare_user(request: Request, identity: UserIdentity) -> None:
    state = request.app.state.web
    state.user_paths(identity.id)
    try:
        state.settings.import_legacy_provider_config(identity.id, state.config_path)
    except Exception:
        pass
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
    return {"user": _identity_payload(request, identity)}


@router.post("/login")
def login(body: LoginRequest, request: Request, response: Response) -> dict[str, object]:
    _origin_guard(request)
    service = request.app.state.web.auth_service
    try:
        identity = service.login(body.email, body.password, request.client.host if request.client else None)
    except Exception as exc:
        raise _error(exc) from exc
    _set_session(request, response, identity)
    return {"user": _identity_payload(request, identity)}


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
    return {"user": _identity_payload(request, identity)}


@router.get("/me")
def me(identity: Annotated[UserIdentity, Depends(require_user)], request: Request) -> dict[str, object]:
    _prepare_user(request, identity)
    return _identity_payload(request, identity)


@router.get("/profile")
def profile(identity: Annotated[UserIdentity, Depends(require_user)], request: Request) -> dict[str, str]:
    return request.app.state.web.settings.profile_for_user(identity.id)


@router.put("/profile")
def update_profile(
    body: ProfilePayload,
    request: Request,
    identity: Annotated[UserIdentity, Depends(require_user)],
) -> dict[str, str]:
    _origin_guard(request)
    return request.app.state.web.settings.update_profile(
        identity.id,
        display_name=body.display_name,
        agent_preferences=body.agent_preferences,
    )


@router.get("/settings")
def settings(identity: Annotated[UserIdentity, Depends(require_user)], request: Request) -> dict[str, object]:
    return request.app.state.web.settings_for_user(identity.id)


@router.put("/agent-config")
def update_agent_config(
    body: AgentConfigPayload,
    request: Request,
    identity: Annotated[UserIdentity, Depends(require_user)],
) -> dict[str, object]:
    _origin_guard(request)
    try:
        return request.app.state.web.settings.update_agent_config(identity.id, body.model_dump(exclude_unset=True))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.put("/provider-config")
def update_provider_config(
    body: ProviderConfigPayload,
    request: Request,
    identity: Annotated[UserIdentity, Depends(require_user)],
) -> dict[str, object]:
    _origin_guard(request)
    try:
        return request.app.state.web.settings.update_provider_config(identity.id, body.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


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
