"""Data model for the source-backed Mini-Agent benchmark suite."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from .metrics import RunMetrics

Capability = Literal["terminal", "software_engineering", "tool_workflow"]
Difficulty = Literal["easy", "medium", "hard"]


@dataclass(frozen=True)
class Budgets:
    """Per-task execution limits passed through to the runtime."""

    max_model_turns: int = 8
    max_tool_calls: int = 32
    max_replans: int = 2
    max_retries: int = 1


@dataclass(frozen=True)
class SeedFile:
    path: str
    content: str


@dataclass(frozen=True)
class SeedSkill:
    name: str
    description: str
    instructions: str


@dataclass(frozen=True)
class SeedMcp:
    server_name: str
    tools: tuple[str, ...] = ()
    profile: str = "retail"


@dataclass(frozen=True)
class Seed:
    files: tuple[SeedFile, ...] = ()
    skills: tuple[SeedSkill, ...] = ()
    mcp: SeedMcp | None = None
    fixture: str | None = None


@dataclass(frozen=True)
class CheckerVerdict:
    """Result of one programmatic checker, normalized to 0..1."""

    score: float
    detail: str = ""
    weight: float = 1.0

    @property
    def passed(self) -> bool:
        return self.score >= 1.0


@dataclass
class CheckContext:
    """Read-only view handed to every checker of a task."""

    task_name: str
    workspace: Path
    status: str
    final_answer: str
    metrics: RunMetrics
    tool_calls_by_name: dict[str, int]


Checker = Callable[[CheckContext], CheckerVerdict]


@dataclass(frozen=True)
class SourceMetadata:
    """Provenance for an adapted upstream benchmark task."""

    benchmark: str
    task_id: str
    url: str
    source_revision: str
    license: str
    adaptation_notes: str


@dataclass(frozen=True)
class BenchmarkTask:
    name: str
    description: str
    capability: Capability
    prompt: str
    difficulty: Difficulty
    source: SourceMetadata
    seed: Seed = Seed()
    checkers: tuple[Checker, ...] = ()
    budgets: Budgets = Budgets()
    tags: tuple[str, ...] = ()
    planner_modes: frozenset[str] = frozenset({"llm"})


@dataclass
class TaskResult:
    task_name: str
    capability: str
    status: str
    score: float | None
    final_answer: str
    metrics: RunMetrics
    verdicts: list[CheckerVerdict]
    error: str | None = None
    run_id: str | None = None
    passed: bool = False
    attempt: int = 1
    trace: list[dict] = field(default_factory=list)
    failure_phase: str | None = None

    def to_dict(self) -> dict:
        return {
            "task_name": self.task_name,
            "capability": self.capability,
            "status": self.status,
            "score": self.score,
            "final_answer": self.final_answer,
            "metrics": self.metrics.to_dict(),
            "verdicts": [
                {"score": verdict.score, "detail": verdict.detail, "weight": verdict.weight}
                for verdict in self.verdicts
            ],
            "error": self.error,
            "run_id": self.run_id,
            "passed": self.passed,
            "attempt": self.attempt,
            "trace": self.trace or [],
            "failure_phase": self.failure_phase,
        }
