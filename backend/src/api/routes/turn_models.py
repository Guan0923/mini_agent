"""Validated request models shared by Turn and Agent Thread routes."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator

from ..chat.routes import RuntimeModelRequest

PermissionMode = Literal["read_only", "workspace_write", "full_access"]
RunningMode = Literal["agent", "plan"]


class TurnExecutionConfig(BaseModel):
    provider_name: str | None = Field(default=None, min_length=1, max_length=80)
    model: RuntimeModelRequest | None = None
    permission_mode: PermissionMode = "read_only"
    running_mode: RunningMode = "agent"
    full_access_acknowledged: StrictBool = False

    @model_validator(mode="after")
    def validate_full_access(self):
        if self.permission_mode == "full_access" and not self.full_access_acknowledged:
            raise ValueError("full_access requires explicit joint file and network confirmation")
        return self


class QueuedDeliveryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    delivery_id: str = Field(min_length=1, max_length=200)
    message_ids: list[str] = Field(min_length=1, max_length=100)


class CreateTurnRequest(TurnExecutionConfig):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=200)
    session_id: str = Field(min_length=1, max_length=200)
    thread_id: str = Field(min_length=1, max_length=200)
    parent_id: str = Field(default="", max_length=200)
    message: dict[str, object] | None = None
    queued_delivery: QueuedDeliveryRequest | None = None

    @model_validator(mode="after")
    def validate_message_source(self):
        if (self.message is None) == (self.queued_delivery is None):
            raise ValueError("message and queued_delivery are mutually exclusive")
        return self


class RewindTurnRequest(TurnExecutionConfig):
    message: dict[str, object]


class CurrentDataRequest(BaseModel):
    current_data_idx: int = Field(ge=0)


class RuntimeModelPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reasoning_effort: Literal["low", "medium", "high", "xhigh", "max"] | None = None
    current_model: str | None = Field(default=None, min_length=1, max_length=500)
    context_length: int | None = Field(default=None, gt=1)
    output_length: int | None = Field(default=None, ge=1)
    thinking: Literal["enable", "disable"] | None = None
    temperature: float | None = Field(default=None, ge=0, le=2)


class SteerTurnRequest(QueuedDeliveryRequest):
    pass


class TurnConfigPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider_name: str | None = Field(default=None, min_length=1, max_length=80)
    model: RuntimeModelPatch | None = None
    permission_mode: PermissionMode | None = None
    running_mode: RunningMode | None = None
    full_access_acknowledged: StrictBool | None = None

    @model_validator(mode="after")
    def validate_full_access(self):
        if self.permission_mode == "full_access" and self.full_access_acknowledged is not True:
            raise ValueError("full_access requires explicit joint file and network confirmation")
        return self


class ForkTurnRequest(BaseModel):
    id: str | None = Field(default=None, min_length=1, max_length=200)
    thread_id: str | None = Field(default=None, min_length=1, max_length=200)


__all__ = [
    "CreateTurnRequest",
    "CurrentDataRequest",
    "ForkTurnRequest",
    "QueuedDeliveryRequest",
    "RewindTurnRequest",
    "RuntimeModelPatch",
    "SteerTurnRequest",
    "TurnConfigPatch",
    "TurnExecutionConfig",
]
