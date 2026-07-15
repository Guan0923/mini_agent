"""Tool contracts shared by the runtime and concrete tools."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Protocol


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
    requires_confirmation: bool = False
    read_only: bool = True


class ToolExecutor(Protocol):
    """The only tool dependency required by the execution runtime."""

    def names(self) -> list[str]: ...

    def read_only_names(self) -> list[str]: ...

    def is_read_only(self, name: str) -> bool: ...

    def invoke(self, name: str, arguments: dict[str, Any], confirmed: bool = False) -> str: ...
