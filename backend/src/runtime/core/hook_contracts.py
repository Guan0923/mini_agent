"""Immutable lifecycle hook inputs, outputs, and operation contracts."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Generic, Literal, TypeVar

from backend.domain import ChatMessage, ToolSpec

from .contracts import InterruptHandler
from .events import RuntimeEvent

HookLifecycle = Literal["run", "model", "tool"]
HookPhase = Literal["before", "after"]
HookStatus = Literal["succeeded", "failed", "cancelled"]
HookDecision = Literal["continue", "reject"]
T = TypeVar("T")


def readonly_mapping(values: Mapping[str, Any] | None = None) -> Mapping[str, Any]:
    """Return a detached, read-only mapping for hook input or output data."""

    return MappingProxyType(deepcopy(dict(values or {})))


@dataclass(frozen=True)
class RunHookInfo:
    session_id: str
    run_id: str
    task: str
    mode: str


@dataclass(frozen=True)
class HookErrorInfo:
    error_type: str
    message: str

    @classmethod
    def from_exception(cls, error: Exception) -> HookErrorInfo:
        return cls(error.__class__.__name__, str(error))


@dataclass(frozen=True)
class HookOutcome(Generic[T]):
    status: HookStatus
    result: T | None = None
    error: HookErrorInfo | None = None


@dataclass(frozen=True)
class RunHookResult:
    status: str
    final_answer: str | None


@dataclass(frozen=True)
class ModelHookResult:
    message: ChatMessage
    usage: dict[str, Any] | None
    response_id: str | None
    model: str | None
    finish_reason: str | None


@dataclass(frozen=True)
class ToolHookResult:
    success: bool
    output: str | None
    error: str | None
    retryable: bool | None


@dataclass(frozen=True)
class RunHookContext:
    run: RunHookInfo
    outcome: HookOutcome[RunHookResult] | None = None


@dataclass(frozen=True)
class ModelHookContext:
    run: RunHookInfo
    operation: str
    exchange_id: str
    output_mode: str
    stream: bool
    messages: Sequence[ChatMessage]
    allowed_tools: Sequence[ToolSpec]
    request_parameters: Mapping[str, Any]
    outcome: HookOutcome[ModelHookResult] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "messages", tuple(deepcopy(self.messages)))
        object.__setattr__(self, "allowed_tools", tuple(deepcopy(self.allowed_tools)))
        object.__setattr__(self, "request_parameters", readonly_mapping(self.request_parameters))


RunEventRecorder = Callable[[str, str, Mapping[str, Any]], None]
HookPublisher = Callable[[RuntimeEvent], None]


@dataclass(frozen=True)
class ToolHookContext:
    run: RunHookInfo
    call_id: str
    name: str
    arguments: Mapping[str, Any]
    workspace_root: str
    project_cwd: str
    permission_mode: str
    requires_confirmation: bool
    read_only: bool
    workspace_confined: bool
    sandbox_launcher: object | None = None
    sandbox_config: Mapping[str, Any] = field(default_factory=readonly_mapping)
    sandbox_user_id: str | None = None
    interrupt: InterruptHandler | None = None
    record_event: RunEventRecorder | None = None
    publish: HookPublisher | None = None
    outcome: HookOutcome[ToolHookResult] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "arguments", readonly_mapping(self.arguments))
        object.__setattr__(self, "sandbox_config", readonly_mapping(self.sandbox_config))


@dataclass(frozen=True)
class HookOperationResult:
    """Explicit result returned by every hook operation."""

    decision: HookDecision
    data: Mapping[str, Any] = field(default_factory=readonly_mapping)
    reason: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "data", MappingProxyType(dict(self.data)))
        if self.decision == "reject" and not (self.reason or "").strip():
            raise ValueError("A rejected hook operation requires a reason.")
        if self.decision == "continue" and self.reason is not None:
            raise ValueError("A continuing hook operation cannot carry a rejection reason.")

    @classmethod
    def continue_execution(cls, data: Mapping[str, Any] | None = None) -> HookOperationResult:
        return cls("continue", data or {})

    @classmethod
    def reject(cls, reason: str, data: Mapping[str, Any] | None = None) -> HookOperationResult:
        return cls("reject", data or {}, reason)


HookOperation = Callable[[Any], HookOperationResult]


__all__ = [
    "HookDecision",
    "HookErrorInfo",
    "HookLifecycle",
    "HookOperation",
    "HookOperationResult",
    "HookOutcome",
    "HookPhase",
    "HookPublisher",
    "HookStatus",
    "ModelHookContext",
    "ModelHookResult",
    "RunHookContext",
    "RunHookInfo",
    "RunHookResult",
    "ToolHookContext",
    "ToolHookResult",
    "readonly_mapping",
]
