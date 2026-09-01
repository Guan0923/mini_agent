"""Process-wide lifecycle hook managers and immutable hook contracts."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

from backend.domain import safe_error_message

from .events import RuntimeEvent
from .hook_contracts import (
    HookErrorInfo,
    HookLifecycle,
    HookOperation,
    HookOperationResult,
    HookOutcome,
    HookPhase,
    HookPublisher,
    ModelHookContext,
    ModelHookResult,
    RunHookContext,
    RunHookInfo,
    RunHookResult,
    ToolHookContext,
    ToolHookResult,
    readonly_mapping,
)


class HookExecutionError(RuntimeError):
    """Stable error raised when one registered operation fails."""

    def __init__(
        self,
        lifecycle: HookLifecycle,
        phase: HookPhase,
        operation: str,
        error: Exception,
    ) -> None:
        super().__init__(safe_error_message(error))
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
                        safe_error_message(error),
                        {
                            **event_data,
                            "error_type": error.__class__.__name__,
                            "error": safe_error_message(error),
                        },
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
from backend.sandbox.control.operation import sandbox_operation  # noqa: E402

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
