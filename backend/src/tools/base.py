"""Tool contracts shared by the runtime and concrete tools."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from backend.domain import DEFAULT_TIME_ZONE, ToolSpec
from backend.domain.state import utc_now


class ToolError(Exception):
    """An expected error produced while invoking a local tool."""


class ConfirmationRequired(ToolError):
    """Raised before a tool performs a potentially destructive action."""


@dataclass(frozen=True)
class ToolInvocationContext:
    """Runtime facts supplied only while executing a tool call."""

    session_id: str | None = None
    timezone: str = DEFAULT_TIME_ZONE
    clock: Callable[[], str] = utc_now
    job_scope: object | None = None
    cancel_requested: Callable[[], bool] | None = None
    permission_mode: str | None = None


ToolHandler = Callable[..., str]


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    handler: ToolHandler
    parameters: dict[str, Any] = field(default_factory=dict)
    requires_confirmation: bool = False
    read_only: bool = True
    retryable: bool = False
    context_handler: ToolHandler | None = None

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(self.name, self.description, self.parameters)


class ToolExecutor(Protocol):
    """The only tool dependency required by the execution runtime."""

    def names(self) -> list[str]: ...

    def read_only_names(self) -> list[str]: ...

    def specs(self) -> list[ToolSpec]: ...

    def read_only_specs(self) -> list[ToolSpec]: ...

    def is_read_only(self, name: str) -> bool: ...

    def requires_confirmation(self, name: str) -> bool: ...

    def is_retryable(self, name: str) -> bool: ...

    def validate_arguments(self, name: str, arguments: dict[str, Any]) -> None: ...

    def invoke(self, name: str, arguments: dict[str, Any], confirmed: bool = False) -> str: ...

    def invoke_with_context(
        self,
        name: str,
        arguments: dict[str, Any],
        context: ToolInvocationContext,
        confirmed: bool = False,
    ) -> str: ...
