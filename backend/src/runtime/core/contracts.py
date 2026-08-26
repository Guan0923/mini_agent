"""Small runtime-level callable contracts shared by orchestration components."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from .events import RuntimeEvent

Confirm = Callable[[str], bool]
EventHandler = Callable[[RuntimeEvent], None]
SteeringHandler = Callable[[], list[object]]
CancellationHandler = Callable[[], bool]
SuspensionHandler = Callable[[], bool]
PlanReviewChoice = Literal["implement", "implement_and_compaction", "stay_in_plan_mode"]
ToolReviewChoice = Literal["continue", "deny", "cancel", "supplement"]
HumanChoice = Literal[
    "implement",
    "implement_and_compaction",
    "stay_in_plan_mode",
    "continue",
    "deny",
    "cancel",
    "supplement",
    "answer",
    "back",
    "trust",
    "skip",
]


@dataclass(frozen=True)
class QuestionOption:
    label: str
    description: str


@dataclass(frozen=True)
class UserQuestion:
    id: str
    header: str
    question: str
    options: tuple[QuestionOption, ...]


@dataclass(frozen=True)
class InterruptRequest:
    """A human decision point for a plan review or confirmed tool action."""

    kind: Literal["plan", "tool", "question", "resume", "skill"]
    message: str
    data: dict[str, Any]
    questions: tuple[UserQuestion, ...] = ()


@dataclass(frozen=True)
class InterruptDecision:
    choice: HumanChoice
    supplement: str | None = None
    answers: dict[str, list[str]] | None = None


InterruptHandler = Callable[[InterruptRequest], InterruptDecision]


class WorkflowModeChanged(RuntimeError):
    """Stop the current workflow before sending or executing with a new mode."""
