"""Registration and invocation policy for all local tools."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from backend.domain import ToolSpec

from .base import ConfirmationRequired, Tool, ToolError


class ToolRegistry:
    """Generic tool registry with no knowledge of concrete tool implementations."""

    def __init__(
        self,
        tools: Iterable[Tool] | Path | None = None,
        *,
        web_search: object | None = None,
        web_fetch: object | None = None,
    ) -> None:
        self._tools: dict[str, Tool] = {}
        self._validators: dict[str, Draft202012Validator] = {}
        if isinstance(tools, Path):
            # Compatibility for callers of the original ``ToolRegistry(workspace)`` API.
            from .catalog import _build_tools

            tools = _build_tools(tools, web_search=web_search, web_fetch=web_fetch)  # type: ignore[arg-type]
        elif web_search is not None or web_fetch is not None:
            raise ValueError("web_search and web_fetch are only supported with the legacy workspace constructor.")
        for tool in tools or ():
            self.register(tool)

    def register(self, tool: Tool) -> None:
        """Register one named capability, rejecting ambiguous duplicates."""

        if tool.name in self._tools:
            raise ValueError(f"Tool already registered: {tool.name}")
        try:
            Draft202012Validator.check_schema(tool.parameters)
        except SchemaError as exc:
            raise ToolError(f"Invalid schema for tool {tool.name!r}: {exc.message}") from exc
        self._tools[tool.name] = tool
        self._validators[tool.name] = Draft202012Validator(tool.parameters)

    def names(self) -> list[str]:
        return list(self._tools)

    def read_only_names(self) -> list[str]:
        return [name for name, tool in self._tools.items() if tool.read_only]

    def specs(self) -> list[ToolSpec]:
        return [tool.spec for tool in self._tools.values()]

    def read_only_specs(self) -> list[ToolSpec]:
        return [tool.spec for tool in self._tools.values() if tool.read_only]

    def is_read_only(self, name: str) -> bool:
        tool = self._tools.get(name)
        if tool is None:
            raise ToolError(f"Unknown tool: {name}")
        return tool.read_only

    def requires_confirmation(self, name: str) -> bool:
        tool = self._tools.get(name)
        if tool is None:
            raise ToolError(f"Unknown tool: {name}")
        return tool.requires_confirmation

    def is_retryable(self, name: str) -> bool:
        tool = self._tools.get(name)
        if tool is None:
            raise ToolError(f"Unknown tool: {name}")
        return tool.retryable

    def invoke(self, name: str, arguments: dict[str, Any], confirmed: bool = False) -> str:
        tool = self._tools.get(name)
        if tool is None:
            raise ToolError(f"Unknown tool: {name}")
        self.validate_arguments(name, arguments)
        if tool.requires_confirmation and not confirmed:
            raise ConfirmationRequired(
                f"{name} requires confirmation before it performs a potentially destructive operation."
            )
        try:
            result = tool.handler(**arguments)
            if not isinstance(result, str):
                raise ToolError(f"Tool {name!r} returned {type(result).__name__}; tool handlers must return text.")
            return result
        except ToolError:
            raise
        except (OSError, TypeError, ValueError) as exc:
            raise ToolError(str(exc)) from exc

    def validate_arguments(self, name: str, arguments: dict[str, Any]) -> None:
        """Validate one call without executing its handler."""

        if name not in self._tools:
            raise ToolError(f"Unknown tool: {name}")
        if not isinstance(arguments, dict):
            raise ToolError(f"Invalid arguments for tool {name!r}: arguments must be an object.")
        error = next(self._validators[name].iter_errors(arguments), None)
        if error is not None:
            raise ToolError(f"Invalid arguments for tool {name!r} at {error.json_path}: {error.message}")
