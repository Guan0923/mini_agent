"""Legacy action values for planners written before ToolMessage."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

ActionType = Literal["tool_call", "final_answer"]


@dataclass(frozen=True)
class AgentAction:
    """Deprecated input adapter for planners written before ToolMessage."""

    type: ActionType
    tool: str | None = None
    arguments: dict[str, Any] = field(default_factory=dict)
    answer: str | None = None
    reasoning: str | None = None
