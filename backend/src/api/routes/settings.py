"""Local profile, runtime, sandbox, and provider settings routes."""

from __future__ import annotations

import json
from typing import Literal
from urllib.parse import urlsplit

import requests
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, StrictBool, StrictInt, field_validator

from backend.domain import DEFAULT_TIME_ZONE, validate_time_zone

router = APIRouter(prefix="/api/settings", tags=["settings"])


class ProfilePayload(BaseModel):
    display_name: str = Field(min_length=1, max_length=80)
    agent_preferences: str = Field(default="", max_length=4000)


class AgentConfigPayload(BaseModel):
    tone: str = Field(default="balanced", max_length=40)
    verbosity: str = Field(default="balanced", max_length=40)
    initiative: str = Field(default="balanced", max_length=40)
    custom_instructions: str = Field(default="", max_length=4000)
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
    port: StrictInt | None = Field(default=None, ge=1, le=65535)


class SandboxConfigPayload(BaseModel):
    network_mode: Literal["no_network", "restricted_network", "full_network"] = "no_network"
    network_allowlist: list[SandboxNetworkRulePayload] = Field(default_factory=list, max_length=128)
    limits: SandboxLimitsPayload = Field(default_factory=SandboxLimitsPayload)


class ProviderConfigPayload(BaseModel):
    provider_name: str = Field(min_length=1, max_length=80)
    protocol: str = Field(default="chat_completions", min_length=1, max_length=40)
    base_url: str = Field(default="", max_length=2000)
    model: str = Field(default="", max_length=300)
    max_tokens: int = Field(default=8192, ge=1, le=384000)
    context_size: int = Field(default=1_024_000, ge=1)
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


def _settings(request: Request):
    return request.app.state.web.settings


def _value_error(exc: ValueError, *, not_found: bool = False) -> HTTPException:
    return HTTPException(status_code=404 if not_found else 422, detail=str(exc))


@router.get("")
def get_settings(request: Request) -> dict[str, object]:
    return request.app.state.web.settings_payload()


@router.get("/profile")
def get_profile(request: Request) -> dict[str, str]:
    return _settings(request).profile()


@router.put("/profile")
def update_profile(body: ProfilePayload, request: Request) -> dict[str, str]:
    try:
        return _settings(request).update_profile(**body.model_dump())
    except ValueError as exc:
        raise _value_error(exc) from exc


@router.put("/agent")
def update_agent(body: AgentConfigPayload, request: Request) -> dict[str, object]:
    try:
        return _settings(request).update_agent_config(body.model_dump())
    except ValueError as exc:
        raise _value_error(exc) from exc


@router.put("/runtime")
def update_runtime(body: RuntimeConfigPayload, request: Request) -> dict[str, object]:
    try:
        return _settings(request).update_runtime_config(body.model_dump())
    except ValueError as exc:
        raise _value_error(exc) from exc


@router.put("/sandbox")
def update_sandbox(body: SandboxConfigPayload, request: Request) -> dict[str, object]:
    try:
        return _settings(request).update_sandbox_config(body.model_dump())
    except ValueError as exc:
        raise _value_error(exc) from exc


@router.put("/providers")
def update_active_provider(body: ProviderConfigPayload, request: Request) -> dict[str, object]:
    try:
        return _settings(request).update_provider_config(body.model_dump())
    except ValueError as exc:
        raise _value_error(exc) from exc


@router.post("/providers", status_code=201)
def add_provider(body: ProviderConfigPayload, request: Request) -> dict[str, object]:
    try:
        return _settings(request).add_provider_config(body.model_dump())
    except ValueError as exc:
        raise _value_error(exc) from exc


@router.patch("/providers/{config_id}")
def patch_provider(config_id: str, body: ProviderConfigPatch, request: Request) -> dict[str, object]:
    try:
        return _settings(request).update_provider_config_by_id(config_id, body.model_dump(exclude_none=True))
    except ValueError as exc:
        raise _value_error(exc, not_found="not found" in str(exc)) from exc


@router.put("/providers/{config_id}/active")
def activate_provider(config_id: str, request: Request) -> dict[str, object]:
    try:
        return _settings(request).activate_provider_config(config_id)
    except ValueError as exc:
        raise _value_error(exc, not_found=True) from exc


@router.delete("/providers/{config_id}")
def delete_provider(config_id: str, request: Request) -> list[dict[str, object]]:
    try:
        return _settings(request).delete_provider_config(config_id)
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
    headers = getattr(response, "headers", None) or {}
    content_length = headers.get("content-length")
    if content_length and int(content_length) > _MAX_MODEL_RESPONSE_BYTES:
        raise HTTPException(status_code=502, detail="模型服务响应过大")
    try:
        chunks: list[bytes] = []
        size = 0
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            chunks.append(chunk)
            size += len(chunk)
            if size > _MAX_MODEL_RESPONSE_BYTES:
                raise HTTPException(status_code=502, detail="模型服务响应过大")
        if chunks:
            return json.loads(b"".join(chunks).decode(getattr(response, "encoding", None) or "utf-8"))
    except (AttributeError, TypeError):
        pass
    return response.json()


@router.post("/providers/models")
def discover_provider_models(body: ProviderModelDiscoveryPayload, request: Request) -> dict[str, list[str]]:
    config = None
    if body.config_id:
        try:
            config = _settings(request).provider_config_for_discovery(body.config_id)
        except ValueError as exc:
            raise HTTPException(status_code=503, detail="提供商密钥暂时不可用，请重新配置。") from exc
        if config is None:
            raise HTTPException(status_code=404, detail="provider configuration not found")
        protocol = str(config.get("protocol") or "chat_completions")
        base_url = str(config.get("base_url") or "")
        api_key = str(body.api_key or "").strip() or str(config.get("api_key") or "")
    else:
        protocol, base_url, api_key = body.protocol, body.base_url, str(body.api_key or "").strip()
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
        if (
            300 <= int(getattr(response, "status_code", 0)) < 400
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


__all__ = ["router"]
