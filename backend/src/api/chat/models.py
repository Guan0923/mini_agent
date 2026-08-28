"""Validated request model and provider-neutral request parameters."""

from __future__ import annotations

from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.providers import ModelConfig, ModelConfigurationError
from backend.storage.settings.crypto import SecretDecryptionError

from ..state import WebAppState

ReasoningEffort = Literal["low", "medium", "high", "xhigh", "max"]


class RuntimeModelRequest(BaseModel):
    """Complete provider-neutral model settings captured at a request boundary."""

    model_config = ConfigDict(extra="forbid")

    reasoning_effort: ReasoningEffort
    current_model: str = Field(min_length=1, max_length=500)
    context_length: int = Field(gt=1)
    output_length: int = Field(ge=1)
    thinking: Literal["enable", "disable"]
    temperature: float = Field(ge=0, le=2)

    @model_validator(mode="after")
    def validate_limits(self) -> RuntimeModelRequest:
        if self.context_length <= self.output_length:
            raise ValueError("model.context_length must be greater than model.output_length")
        return self


def _reasoning_parameters(effort: ReasoningEffort) -> dict[str, object]:
    return {"thinking": {"type": "enabled"}, "reasoning_effort": effort}


def _model_request_parameters(model: RuntimeModelRequest | None, fallback: ReasoningEffort) -> dict[str, object]:
    """Translate the complete public model object into provider request options."""

    if model is None:
        return _reasoning_parameters(fallback)
    if model.thinking == "disable":
        return {
            "thinking": {"type": "disabled"},
            "max_tokens": model.output_length,
            "temperature": model.temperature,
        }
    return {
        "thinking": {"type": "enabled"},
        "reasoning_effort": model.reasoning_effort,
        "max_tokens": model.output_length,
        "temperature": model.temperature,
    }


def _model_config_snapshot(
    state: WebAppState,
    *,
    provider_name: str | None = None,
) -> ModelConfig:
    try:
        if provider_name and provider_name != "unknown":
            return state.model_config(provider_name)
        return state.model_config()
    except SecretDecryptionError as exc:
        raise HTTPException(
            status_code=409,
            detail="当前提供商密钥无法解密，请在用户设置中重新填写 API Key。",
        ) from exc
    except ModelConfigurationError as exc:
        raise HTTPException(status_code=422, detail=f"模型未配置：{exc}") from exc
