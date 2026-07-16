"""Tool contracts shared by the runtime and concrete tools."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from mini_agent.domain import ToolSpec


class ToolError(Exception):
    """An expected error produced while invoking a local tool."""


class ConfirmationRequired(ToolError):
    """Raised before a tool performs a potentially destructive action."""


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
