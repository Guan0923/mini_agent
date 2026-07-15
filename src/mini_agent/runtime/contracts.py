"""Small runtime-level callable contracts shared by orchestration components."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from .events import RuntimeEvent

Confirm = Callable[[str], bool]
EventHandler = Callable[[RuntimeEvent], None]
HumanChoice = Literal["continue", "cancel", "supplement"]


@dataclass(frozen=True)
class InterruptRequest:
    """A human-approval point emitted before a plan or tool action proceeds."""

    kind: Literal["plan", "tool"]
    message: str
    data: dict[str, Any]


@dataclass(frozen=True)
class InterruptDecision:
    choice: HumanChoice
    supplement: str | None = None


InterruptHandler = Callable[[InterruptRequest], InterruptDecision]
