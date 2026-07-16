"""Small runtime-level callable contracts shared by orchestration components."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from .events import RuntimeEvent

Confirm = Callable[[str], bool]
EventHandler = Callable[[RuntimeEvent], None]
SteeringHandler = Callable[[], list[str]]
PlanReviewChoice = Literal["implement", "implement_clear_session", "cancel"]
ToolReviewChoice = Literal["continue", "cancel", "supplement"]
HumanChoice = Literal["implement", "implement_clear_session", "continue", "cancel", "supplement"]


@dataclass(frozen=True)
class InterruptRequest:
    """A human decision point for a plan review or confirmed tool action."""

    kind: Literal["plan", "tool"]
    message: str
    data: dict[str, Any]


@dataclass(frozen=True)
class InterruptDecision:
    choice: HumanChoice
    supplement: str | None = None


InterruptHandler = Callable[[InterruptRequest], InterruptDecision]
