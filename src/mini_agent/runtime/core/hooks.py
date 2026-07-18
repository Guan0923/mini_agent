"""Synchronous lifecycle hooks with controlled, provider-neutral contexts."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Generic, Literal, TypeVar

from mini_agent.domain import ChatMessage, ToolSpec

from .events import RuntimeEvent

HookLifecycle = Literal["run", "model", "tool"]
HookStatus = Literal["succeeded", "failed", "cancelled"]
T = TypeVar("T")


@dataclass(frozen=True)
class RunHookInfo:
    session_id: str
    run_id: str
    task: str
    mode: str


class _CancellableContext:
    cancellation_reason: str | None

    def cancel(self, reason: str) -> None:
        value = reason.strip()
        if not value:
            raise ValueError("Hook cancellation requires a non-empty reason.")
        self.cancellation_reason = value


@dataclass
class RunHookContext(_CancellableContext):
    run: RunHookInfo
    cancellation_reason: str | None = field(default=None, init=False)


@dataclass
class ModelHookContext(_CancellableContext):
    run: RunHookInfo
    operation: str
    exchange_id: str
    output_mode: str
    stream: bool
    messages: list[ChatMessage]
    allowed_tools: list[ToolSpec]
    request_parameters: dict[str, Any]
    cancellation_reason: str | None = field(default=None, init=False)


@dataclass
class ToolHookContext(_CancellableContext):
    run: RunHookInfo
    call_id: str
    name: str
    arguments: dict[str, Any]
    cancellation_reason: str | None = field(default=None, init=False)


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
    strategy: str | None
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


class AgentHook:
    """Override only the lifecycle methods needed by one extension."""

    name: str | None = None

    def before_run(self, context: RunHookContext) -> None:
        pass

    def after_run(self, context: RunHookContext, outcome: HookOutcome[RunHookResult]) -> None:
        pass

    def before_model(self, context: ModelHookContext) -> None:
        pass

    def after_model(self, context: ModelHookContext, outcome: HookOutcome[ModelHookResult]) -> None:
        pass

    def before_tool(self, context: ToolHookContext) -> None:
        pass

    def after_tool(self, context: ToolHookContext, outcome: HookOutcome[ToolHookResult]) -> None:
        pass


class HookCancellation(RuntimeError):
    def __init__(self, lifecycle: HookLifecycle, hook: str, reason: str) -> None:
        super().__init__(f"{lifecycle.capitalize()} cancelled by hook {hook!r}: {reason}")
        self.lifecycle = lifecycle
        self.hook = hook
        self.reason = reason


class HookExecutionError(RuntimeError):
    def __init__(self, lifecycle: HookLifecycle, phase: str, hook: str, error: Exception) -> None:
        super().__init__(f"Hook {hook!r} failed during {phase}_{lifecycle}.")
        self.lifecycle = lifecycle
        self.phase = phase
        self.hook = hook
        self.error_type = error.__class__.__name__


HookPublisher = Callable[[RuntimeEvent], None]
HookOperation = Callable[[Any], T]
OutcomeFactory = Callable[[T], HookOutcome[Any]]


class HookManager:
    """Run hooks as a middleware stack around one lifecycle operation."""

    def __init__(self, hooks: Iterable[AgentHook] = ()) -> None:
        self._hooks = tuple(hooks)

    def run_run(
        self,
        context: RunHookContext,
        operation: Callable[[RunHookContext], T],
        outcome: OutcomeFactory,
        publish: HookPublisher,
    ) -> T:
        return self._run("run", context, operation, outcome, publish)

    def run_model(
        self,
        context: ModelHookContext,
        operation: Callable[[ModelHookContext], T],
        outcome: OutcomeFactory,
        publish: HookPublisher,
    ) -> T:
        return self._run("model", context, operation, outcome, publish)

    def run_tool(
        self,
        context: ToolHookContext,
        operation: Callable[[ToolHookContext], T],
        outcome: OutcomeFactory,
        publish: HookPublisher,
    ) -> T:
        return self._run("tool", context, operation, outcome, publish)

    def _run(
        self,
        lifecycle: HookLifecycle,
        context: Any,
        operation: HookOperation[T],
        outcome_factory: OutcomeFactory,
        publish: HookPublisher,
    ) -> T:
        entered: list[AgentHook] = []
        for hook in self._hooks:
            before_name = f"before_{lifecycle}"
            after_name = f"after_{lifecycle}"
            if not self._implements(hook, before_name) and not self._implements(hook, after_name):
                continue
            try:
                if self._implements(hook, before_name):
                    self._invoke(hook, "before", lifecycle, context, publish)
            except HookExecutionError as error:
                self._after(
                    entered,
                    lifecycle,
                    context,
                    HookOutcome(status="failed", error=HookErrorInfo(error.error_type, str(error))),
                    publish,
                    suppress_errors=True,
                )
                raise
            entered.append(hook)
            if context.cancellation_reason is not None:
                cancellation = HookCancellation(
                    lifecycle,
                    self._name(hook),
                    context.cancellation_reason,
                )
                self._after(
                    entered,
                    lifecycle,
                    context,
                    HookOutcome(status="cancelled", error=HookErrorInfo.from_exception(cancellation)),
                    publish,
                )
                raise cancellation

        try:
            result = operation(context)
        except Exception as error:
            status: HookStatus = "cancelled" if isinstance(error, HookCancellation) else "failed"
            self._after(
                entered,
                lifecycle,
                context,
                HookOutcome(status=status, error=HookErrorInfo.from_exception(error)),
                publish,
            )
            raise

        self._after(entered, lifecycle, context, outcome_factory(result), publish)
        return result

    def _after(
        self,
        hooks: list[AgentHook],
        lifecycle: HookLifecycle,
        context: Any,
        outcome: HookOutcome[Any],
        publish: HookPublisher,
        *,
        suppress_errors: bool = False,
    ) -> None:
        first_error: HookExecutionError | None = None
        for hook in reversed(hooks):
            if not self._implements(hook, f"after_{lifecycle}"):
                continue
            try:
                self._invoke(hook, "after", lifecycle, deepcopy(context), publish, deepcopy(outcome))
            except HookExecutionError as error:
                first_error = first_error or error
        if first_error is not None and not suppress_errors:
            raise first_error

    @staticmethod
    def _name(hook: AgentHook) -> str:
        return hook.name or hook.__class__.__name__

    @staticmethod
    def _implements(hook: AgentHook, method_name: str) -> bool:
        return getattr(type(hook), method_name) is not getattr(AgentHook, method_name)

    def _invoke(
        self,
        hook: AgentHook,
        phase: Literal["before", "after"],
        lifecycle: HookLifecycle,
        context: Any,
        publish: HookPublisher,
        outcome: HookOutcome[Any] | None = None,
    ) -> None:
        name = self._name(hook)
        method_name = f"{phase}_{lifecycle}"
        data = {"hook": name, "phase": phase, "lifecycle": lifecycle}
        publish(RuntimeEvent("hook_started", method_name, data))
        try:
            method = getattr(hook, method_name)
            if outcome is None:
                method(context)
            else:
                method(context, outcome)
        except Exception as error:
            publish(
                RuntimeEvent(
                    "hook_failed",
                    method_name,
                    {**data, "error_type": error.__class__.__name__},
                )
            )
            raise HookExecutionError(lifecycle, phase, name, error) from error
        publish(RuntimeEvent("hook_completed", method_name, data))
