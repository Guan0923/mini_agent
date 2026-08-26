"""Process-wide lifecycle hook managers and immutable hook contracts."""

from __future__ import annotations

from abc import ABC, abstractmethod
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
    permission_mode: str
    requires_confirmation: bool
    read_only: bool
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


class HookExecutionError(RuntimeError):
    """Stable error raised when one registered operation fails."""

    def __init__(
        self,
        lifecycle: HookLifecycle,
        phase: HookPhase,
        operation: str,
        error: Exception,
    ) -> None:
        super().__init__(f"Hook operation {operation!r} failed during {phase}_{lifecycle}.")
        self.lifecycle = lifecycle
        self.phase = phase
        self.operation = operation
        self.hook = operation
        self.error_type = error.__class__.__name__


class HookRejected(RuntimeError):
    """Raised by a lifecycle boundary when its before manager rejects."""

    def __init__(self, lifecycle: HookLifecycle, reason: str, data: Mapping[str, Any]) -> None:
        super().__init__(reason)
        self.lifecycle = lifecycle
        self.reason = reason
        self.data = MappingProxyType(dict(data))


class HookManager(ABC):
    """Abstract manager for one process-wide lifecycle boundary."""

    @property
    @abstractmethod
    def operations(self) -> tuple[HookOperation, ...]: ...

    @abstractmethod
    def register(self, operation: HookOperation) -> None: ...

    @abstractmethod
    def execute(self, context: Any, publish: HookPublisher | None = None) -> HookOperationResult: ...


class SequentialHookManager(HookManager):
    """Execute registered operations once in strict FIFO registration order."""

    def __init__(self, phase: HookPhase, lifecycle: HookLifecycle) -> None:
        self.phase = phase
        self.lifecycle = lifecycle
        self._operations: list[HookOperation] = []

    @property
    def operations(self) -> tuple[HookOperation, ...]:
        return tuple(self._operations)

    def register(self, operation: HookOperation) -> None:
        if not callable(operation):
            raise TypeError("Hook operation must be callable.")
        self._operations.append(operation)

    def execute(self, context: Any, publish: HookPublisher | None = None) -> HookOperationResult:
        emit = publish or (lambda _event: None)
        accumulated: dict[str, Any] = {}
        for operation in self._operations:
            name = self._name(operation)
            event_data = {
                "hook": name,
                "operation": name,
                "phase": self.phase,
                "lifecycle": self.lifecycle,
            }
            event_name = f"{self.phase}_{self.lifecycle}"
            emit(RuntimeEvent("hook_started", event_name, event_data))
            try:
                result = operation(context)
                if not isinstance(result, HookOperationResult):
                    raise TypeError("Hook operation must return HookOperationResult.")
            except Exception as error:
                emit(
                    RuntimeEvent(
                        "hook_failed",
                        event_name,
                        {**event_data, "error_type": error.__class__.__name__},
                    )
                )
                raise HookExecutionError(self.lifecycle, self.phase, name, error) from error
            accumulated.update(result.data)
            emit(
                RuntimeEvent(
                    "hook_completed",
                    event_name,
                    {**event_data, "decision": result.decision},
                )
            )
            if result.decision == "reject":
                return HookOperationResult.reject(result.reason or "Hook operation rejected.", accumulated)
        return HookOperationResult.continue_execution(accumulated)

    @staticmethod
    def _name(operation: HookOperation) -> str:
        explicit = getattr(operation, "name", None)
        if isinstance(explicit, str) and explicit:
            return explicit
        function_name = getattr(operation, "__name__", None)
        if isinstance(function_name, str) and function_name:
            return function_name
        return operation.__class__.__name__


before_run_hook_manager = SequentialHookManager("before", "run")
after_run_hook_manager = SequentialHookManager("after", "run")
before_model_hook_manager = SequentialHookManager("before", "model")
after_model_hook_manager = SequentialHookManager("after", "model")
before_tool_hook_manager = SequentialHookManager("before", "tool")
after_tool_hook_manager = SequentialHookManager("after", "tool")


# Sandbox is an ordinary operation. Importing it only after all contracts and
# global managers exist guarantees that no later registration can precede it.
from backend.sandbox.operation import sandbox_operation  # noqa: E402

before_tool_hook_manager.register(sandbox_operation)


__all__ = [
    "HookErrorInfo",
    "HookExecutionError",
    "HookManager",
    "HookOperation",
    "HookOperationResult",
    "HookOutcome",
    "HookRejected",
    "ModelHookContext",
    "ModelHookResult",
    "RunHookContext",
    "RunHookInfo",
    "RunHookResult",
    "SequentialHookManager",
    "ToolHookContext",
    "ToolHookResult",
    "after_model_hook_manager",
    "after_run_hook_manager",
    "after_tool_hook_manager",
    "before_model_hook_manager",
    "before_run_hook_manager",
    "before_tool_hook_manager",
    "readonly_mapping",
]
