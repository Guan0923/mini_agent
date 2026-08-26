"""Public browser authentication and terminal device-authorization routes."""

from __future__ import annotations

import json
import os
import sqlite3
from typing import Annotated, Literal
from urllib.parse import urlsplit

import requests
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, StrictBool, StrictInt, field_validator

from backend.cloud import CloudApiError, CloudAuthExpired, CloudConflict, CloudUnavailable
from backend.domain import DEFAULT_TIME_ZONE, validate_time_zone
from backend.storage.auth.types import AuthStorageUnavailable
from backend.tools.terminal import available_terminal_executables

from ..user_data import UserDataUnavailable, remove_user_root, user_root
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


class GuestImportRequest(BaseModel):
    decision: Literal["import", "dismiss"]


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


class RuntimeConfigPayload(BaseModel):
    max_tool_calls: StrictInt = Field(default=32, ge=1, le=1000)
    terminal_type: Literal["cmd", "git_bash", "powershell", "pwsh", "wsl"] = "cmd"


class SandboxLimitsPayload(BaseModel):
    wall_seconds: StrictInt = Field(default=300, ge=1, le=300)
    cpu_seconds: StrictInt = Field(default=300, ge=1, le=300)
    memory_mib: StrictInt = Field(default=4096, ge=128, le=4096)
    processes: StrictInt = Field(default=256, ge=1, le=256)
    handles: StrictInt = Field(default=16384, ge=64, le=16384)
    output_chars: StrictInt = Field(default=20000, ge=1000, le=20000)
    disk_mib: StrictInt = Field(default=0, ge=0, le=20 * 1024)


class SandboxNetworkRulePayload(BaseModel):
    host: str = Field(min_length=1, max_length=253)
    port: StrictInt = Field(ge=1, le=65535)


class SandboxConfigPayload(BaseModel):
    enabled: Literal[True] = True
    file_mode: Literal["read_only", "workspace_write", "full_access"] = "read_only"
    network_mode: Literal["no_network", "restricted_network", "full_network"] = "no_network"
    network_allowlist: list[SandboxNetworkRulePayload] = Field(default_factory=list, max_length=128)
    limits: SandboxLimitsPayload = Field(default_factory=SandboxLimitsPayload)
    full_access_acknowledged: StrictBool = False


class ProviderConfigPayload(BaseModel):
    provider_name: str = Field(min_length=1, max_length=80)
    protocol: str = Field(default="chat_completions", min_length=1, max_length=40)
    base_url: str = Field(default="", max_length=2000)
    model: str = Field(default="", max_length=300)
    max_tokens: int = Field(default=8192, ge=1, le=384000)
    context_size: int = Field(default=1024000, ge=1)
    tokenizer_model: str = Field(default="", max_length=300)
    api_key: str | None = Field(default=None, max_length=4096)


class ProviderConfigPatch(BaseModel):
    provider_name: str | None = Field(default=None, min_length=1, max_length=80)
    model: str | None = Field(default=None, max_length=300)
    api_key: str | None = Field(default=None, max_length=4096)


class ProviderModelDiscoveryPayload(BaseModel):
    config_id: str | None = Field(default=None, max_length=160)
    provider_name: str = Field(default="default", min_length=1, max_length=80)
    protocol: str = Field(default="chat_completions", min_length=1, max_length=40)
    base_url: str = Field(default="", max_length=2000)
    api_key: str | None = Field(default=None, max_length=4096)


def _error(exc: Exception) -> HTTPException:
    if isinstance(exc, CloudAuthExpired):
        return HTTPException(
            status_code=401,
            detail=str(exc),
            headers={"WWW-Authenticate": "Bearer"},
        )
    if isinstance(exc, CloudConflict):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, CloudUnavailable):
        return HTTPException(status_code=503, detail=str(exc))
    if isinstance(exc, CloudApiError):
        return HTTPException(status_code=exc.status_code or 502, detail=str(exc))
    if isinstance(exc, UserDataUnavailable) or isinstance(exc, (OSError, sqlite3.Error)):
        return HTTPException(status_code=503, detail="本地用户数据目录暂不可用，请检查服务器权限。")
    if isinstance(exc, ValueError) and exc.__class__.__module__.startswith("backend.configuration"):
        return HTTPException(status_code=503, detail="本地用户数据配置无效，请运行存储重置流程。")
    if isinstance(exc, AuthStorageUnavailable):
        return HTTPException(status_code=503, detail="认证与用户设置服务暂不可用。")
    if isinstance(exc, RateLimitError):
        return HTTPException(status_code=429, detail=str(exc), headers={"Retry-After": str(exc.retry_after)})
    if isinstance(exc, MailDeliveryError):
        return HTTPException(status_code=503, detail=str(exc))
    if isinstance(exc, AuthError):
        return HTTPException(status_code=400, detail=str(exc))
    return HTTPException(status_code=500, detail="服务暂时无法处理请求。")


def _origin_guard(request: Request) -> None:
    if not request.app.state.web.auth_service.origin_allowed(request):
        raise HTTPException(status_code=403, detail="不允许的请求来源。")


def _identity_payload(request: Request, identity: UserIdentity) -> dict[str, object]:
    pending_reader = getattr(request.app.state.web.auth, "pending_guest_import", None)
    pending = pending_reader(identity.id) if callable(pending_reader) else None
    profile = request.app.state.web.settings.ensure_profile(
        identity.id,
        display_name_default="游客用户" if identity.is_guest else (identity.email or "用户"),
    )
    return {
        "id": identity.id,
        "email": identity.email,
        "kind": identity.kind,
        "guest_import": pending,
        **profile,
    }


def _prepare_user(request: Request, identity: UserIdentity, *, first_cloud_login: bool = False) -> None:
    state = request.app.state.web
    from ..user_data import user_root

    # Observe whether a local settings database existed before initialization;
    # ``user_paths`` itself creates the canonical tree and must not mask the
    # first-login cloud restore decision.
    local_user_db = user_root(state.data_root, identity.id) / "user.db"
    had_local_user_db = local_user_db.is_file()
    state.user_paths(identity.id)
    manager = state.event_sync_manager
    if manager is None or identity.is_guest:
        return
    try:
        # Event synchronization performs encrypted pull/replay on demand;
        # there is no snapshot list or ZIP restore step during login.
        manager.recover_key_if_available(identity.id)
        if first_cloud_login or not had_local_user_db:
            manager.sync_now(identity.id, force=False)
    except Exception:
        # Authentication and local usage remain available while the sync page
        # exposes any cloud/key recovery failure for explicit user action.
        return


def _set_session(request: Request, response: Response, identity: UserIdentity) -> None:
    service = request.app.state.web.auth_service
    source = service.identity_from_request(request)
    first_cloud_login = bool(getattr(service, "consume_first_cloud_login", lambda _id: False)(identity.id))
    _prepare_user(request, identity, first_cloud_login=first_cloud_login)
    token = service.browser_session(identity)
    if source is not None and source[0].is_guest and not identity.is_guest:
        setter = getattr(service.store, "set_pending_guest_import", None)
        if callable(setter):
            setter(identity.id, source[0].id)
    service.set_browser_cookie(response, token)


@router.post("/guest")
def guest(request: Request, response: Response) -> dict[str, object]:
    _origin_guard(request)
    service = request.app.state.web.auth_service
    created_guest = False
    created_root = None
    root_existed = True
    identity = None
    try:
        existing = service.identity_from_request(request)
        identity, created_guest = service.get_or_create_guest()
        if existing is not None and existing[0].is_guest and existing[0].id == identity.id and existing[1] == "browser":
            identity, token = identity, existing[2]
        else:
            ip_address = request.client.host if request.client else "unknown"
            if not service.store.consume_limit(f"ip:{ip_address}", "guest:hour", 20, 3600):
                raise RateLimitError("游客登录请求过于频繁，请稍后再试。", 3600)
            created_root = user_root(request.app.state.web.data_root, identity.id)
            root_existed = created_root.exists()
            request.app.state.web.user_paths(identity.id)
            identity, token = service.guest_session(identity)
        service.set_browser_cookie(response, token)
        request.app.state.web.user_paths(identity.id)
        return {"user": _identity_payload(request, identity)}
    except Exception as exc:
        if created_guest and identity is not None:
            try:
                service.store.delete_guest(identity.id)
            except Exception:
                pass
            if not root_existed and created_root is not None:
                try:
                    remove_user_root(request.app.state.web.data_root, identity.id)
                except Exception:
                    pass
        raise _error(exc) from exc


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
        _set_session(request, response, identity)
    except Exception as exc:
        raise _error(exc) from exc
    return {"user": _identity_payload(request, identity)}


@router.post("/login")
def login(body: LoginRequest, request: Request, response: Response) -> dict[str, object]:
    _origin_guard(request)
    service = request.app.state.web.auth_service
    try:
        identity = service.login(body.email, body.password, request.client.host if request.client else None)
        _set_session(request, response, identity)
    except Exception as exc:
        raise _error(exc) from exc
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
    return request.app.state.web.settings.ensure_profile(
        identity.id,
        display_name_default="游客用户" if identity.is_guest else (identity.email or "用户"),
    )


@router.put("/profile")
def update_profile(
    body: ProfilePayload,
    request: Request,
    identity: Annotated[UserIdentity, Depends(require_user)],
) -> dict[str, str]:
    _origin_guard(request)
    try:
        return request.app.state.web.settings.update_profile(
            identity.id,
            display_name=body.display_name,
            agent_preferences=body.agent_preferences,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/settings")
def settings(identity: Annotated[UserIdentity, Depends(require_user)], request: Request) -> dict[str, object]:
    return request.app.state.web.settings_for_user(identity.id)


@router.put("/sandbox-config")
def update_sandbox_config(
    body: SandboxConfigPayload,
    request: Request,
    identity: Annotated[UserIdentity, Depends(require_user)],
) -> dict[str, object]:
    _origin_guard(request)
    try:
        return request.app.state.web.settings.update_sandbox_config(identity.id, body.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _broker_payload(value: object) -> dict[str, object]:
    if callable(getattr(value, "to_dict", None)):
        return dict(value.to_dict())
    if isinstance(value, dict):
        return dict(value)
    return {"installed": False, "healthy": False, "detail": "Broker returned an invalid status"}


@router.get("/guest-import")
def guest_import_status(
    identity: Annotated[UserIdentity, Depends(require_user)], request: Request
) -> dict[str, object]:
    if identity.is_guest:
        return {"available": False, "pending": None}
    pending = request.app.state.web.auth.pending_guest_import(identity.id)
    return {"available": pending is not None, "pending": pending}


@router.post("/guest-import")
def guest_import(
    body: GuestImportRequest,
    request: Request,
    identity: Annotated[UserIdentity, Depends(require_user)],
) -> dict[str, object]:
    _origin_guard(request)
    if identity.is_guest:
        raise HTTPException(status_code=403, detail="游客身份不能导入游客会话。")
    pending = request.app.state.web.auth.pending_guest_import(identity.id)
    if pending is None:
        return {"status": "none", "imported": [], "skipped": [], "count": 0, "sync_count": 0, "projects_imported": []}
    if body.decision == "dismiss":
        request.app.state.web.auth.finish_guest_import(identity.id, "dismiss")
        return {
            "status": "dismissed",
            "imported": [],
            "skipped": [],
            "count": 0,
            "sync_count": 0,
            "projects_imported": [],
        }
    from .. import user_data

    source_identity = request.app.state.web.auth.user_by_id(str(pending["guest_id"]))
    if source_identity is None or not source_identity.is_guest:
        raise HTTPException(status_code=409, detail="待导入的游客身份已不可用。")
    try:
        result = user_data.import_guest_sessions(
            request.app.state.web.data_root,
            str(pending["guest_id"]),
            identity.id,
        )
    except (RuntimeError, OSError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    request.app.state.web.auth.finish_guest_import(identity.id, "import")
    if result.get("sync_count", result["count"]):
        marker = request.app.state.web.settings
        mark_dirty = getattr(marker, "mark_dirty", None)
        if callable(mark_dirty):
            mark_dirty(identity.id)
    return {"status": "imported", **result}


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


@router.put("/runtime-config")
def update_runtime_config(
    body: RuntimeConfigPayload,
    request: Request,
    identity: Annotated[UserIdentity, Depends(require_user)],
) -> dict[str, object]:
    _origin_guard(request)
    try:
        values = body.model_dump(exclude_unset=True)
        if os.name == "nt" and body.terminal_type not in available_terminal_executables(is_windows=True):
            raise ValueError("selected terminal is not available on this system")
        return request.app.state.web.settings.update_runtime_config(identity.id, values)
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


@router.post("/provider-configs")
def add_provider_config(
    body: ProviderConfigPayload,
    request: Request,
    identity: Annotated[UserIdentity, Depends(require_user)],
) -> dict[str, object]:
    _origin_guard(request)
    try:
        return request.app.state.web.settings.add_provider_config(identity.id, body.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.patch("/provider-configs/{config_id}")
def patch_provider_config(
    config_id: str,
    body: ProviderConfigPatch,
    request: Request,
    identity: Annotated[UserIdentity, Depends(require_user)],
) -> dict[str, object]:
    _origin_guard(request)
    try:
        return request.app.state.web.settings.update_provider_config_by_id(
            identity.id, config_id, body.model_dump(exclude_none=True)
        )
    except ValueError as exc:
        raise HTTPException(status_code=404 if "not found" in str(exc) else 422, detail=str(exc)) from exc


@router.put("/provider-configs/{config_id}/active")
def activate_provider_config(
    config_id: str,
    request: Request,
    identity: Annotated[UserIdentity, Depends(require_user)],
) -> dict[str, object]:
    _origin_guard(request)
    try:
        return request.app.state.web.settings.activate_provider_config(identity.id, config_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.delete("/provider-configs/{config_id}")
def delete_provider_config(
    config_id: str,
    request: Request,
    identity: Annotated[UserIdentity, Depends(require_user)],
) -> list[dict[str, object]]:
    _origin_guard(request)
    try:
        return request.app.state.web.settings.delete_provider_config(identity.id, config_id)
    except ValueError as exc:
        status = 409 if "activate another" in str(exc) else 404
        raise HTTPException(status_code=status, detail=str(exc)) from exc


def _models_endpoint(base_url: str) -> str:
    parsed = urlsplit(base_url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("base_url must be an absolute HTTP(S) URL")
    path = parsed.path.rstrip("/")
    for suffix in ("/chat/completions", "/responses", "/messages", "/models"):
        if path.endswith(suffix):
            path = path[: -len(suffix)].rstrip("/")
            break
    if not path.endswith("/v1"):
        path = f"{path}/v1" if path else "/v1"
    return parsed._replace(path=f"{path}/models", query="", fragment="").geturl()


_MAX_MODEL_RESPONSE_BYTES = 2 * 1024 * 1024


def _model_response_json(response: requests.Response) -> object:
    """Decode a bounded model-list response without retaining an unbounded body."""

    headers = getattr(response, "headers", None) or {}
    content_length = headers.get("content-length")
    if content_length and int(content_length) > _MAX_MODEL_RESPONSE_BYTES:
        raise HTTPException(status_code=502, detail="模型服务响应过大")
    try:
        chunks: list[bytes] = []
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            chunks.append(chunk)
            if sum(len(item) for item in chunks) > _MAX_MODEL_RESPONSE_BYTES:
                raise HTTPException(status_code=502, detail="模型服务响应过大")
        if chunks:
            return json.loads(b"".join(chunks).decode(getattr(response, "encoding", None) or "utf-8"))
    except (AttributeError, TypeError):
        # Small mocked responses and compatible response implementations may
        # expose only ``json``; requests.Response uses the bounded path above.
        pass
    return response.json()


@router.post("/provider-models/discover")
def discover_provider_models(
    body: ProviderModelDiscoveryPayload,
    request: Request,
    identity: Annotated[UserIdentity, Depends(require_user)],
) -> dict[str, list[str]]:
    _origin_guard(request)
    config = None
    if body.config_id:
        try:
            config = request.app.state.web.settings.provider_config_for_discovery(identity.id, body.config_id)
        except ValueError as exc:
            raise HTTPException(status_code=503, detail="提供商密钥暂时不可用，请重新配置。") from exc
        if config is None:
            raise HTTPException(status_code=404, detail="provider configuration not found")
        protocol = str(config.get("protocol") or "chat_completions")
        base_url = str(config.get("base_url") or "")
        api_key = str(body.api_key or "").strip() or str(config.get("api_key") or "")
    else:
        protocol, base_url, api_key = (
            body.protocol,
            body.base_url,
            str(body.api_key or "").strip(),
        )
    if protocol not in {"chat_completions", "responses", "messages"}:
        raise HTTPException(status_code=422, detail="不支持的提供商协议")
    try:
        endpoint = _models_endpoint(base_url)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    headers = {"Accept": "application/json"}
    if api_key:
        if protocol == "messages":
            headers.update({"x-api-key": api_key, "anthropic-version": "2023-06-01"})
        else:
            headers["Authorization"] = f"Bearer {api_key}"
    try:
        response = requests.get(endpoint, headers=headers, timeout=10, allow_redirects=False)
        status_code = getattr(response, "status_code", None)
        if (
            (isinstance(status_code, int) and 300 <= status_code < 400)
            or getattr(response, "is_redirect", False)
            or getattr(response, "is_permanent_redirect", False)
        ):
            raise HTTPException(status_code=502, detail="模型服务不允许重定向")
        response.raise_for_status()
        payload = _model_response_json(response)
    except HTTPException:
        raise
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail="获取模型列表失败，请检查 Base URL 和 API Key") from exc
    except ValueError as exc:
        raise HTTPException(status_code=502, detail="模型服务返回的不是有效 JSON") from exc
    values = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(values, list):
        values = payload.get("models") if isinstance(payload, dict) else []
    models: list[str] = []
    for item in values[:500] if isinstance(values, list) else []:
        value = item.get("id") if isinstance(item, dict) else item
        if isinstance(value, str) and value.strip() and value.strip() not in models:
            models.append(value.strip())
    return {"models": models}


@router.post("/logout")
def logout(
    request: Request, response: Response, identity: Annotated[UserIdentity, Depends(require_user)]
) -> dict[str, str]:
    _origin_guard(request)
    token = getattr(request.state, "auth_token", None)
    if isinstance(token, str):
        request.app.state.web.auth_service.revoke_token(identity, token)
    request.app.state.web.auth_service.clear_browser_cookie(response)
    return {"detail": "已退出登录。"}


@router.post("/device/start", response_model=DeviceStartResponse)
def device_start(request: Request) -> DeviceStartResponse:
    _origin_guard(request)
    service = request.app.state.web.auth_service
    server_url = str(request.base_url).rstrip("/")
    try:
        poll, browser, expires = service.start_device(server_url)
    except Exception as exc:
        raise _error(exc) from exc
    return DeviceStartResponse(
        poll_secret=poll,
        verification_url=service.device_url(browser),
        expires_in=expires,
    )


@router.get("/device/info")
def device_info(grant: str, request: Request) -> dict[str, object]:
    try:
        info = request.app.state.web.auth_service.device_info(grant)
    except Exception as exc:
        raise _error(exc) from exc
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
    try:
        accepted = request.app.state.web.auth_service.approve_device(body.grant, identity.id, body.approved)
    except Exception as exc:
        raise _error(exc) from exc
    if not accepted:
        raise HTTPException(status_code=410, detail="设备授权请求不存在、已处理或已过期。")
    return {"status": "approved" if body.approved else "denied"}


@router.post("/device/token")
def device_token(body: DevicePollRequest, request: Request) -> JSONResponse:
    try:
        status_name, token = request.app.state.web.auth_service.poll_device(body.poll_secret)
    except Exception as exc:
        raise _error(exc) from exc
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
