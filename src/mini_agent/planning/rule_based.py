"""A transparent offline baseline planner."""

from __future__ import annotations

import re
from collections.abc import Callable

from mini_agent.domain import AgentAction, ExecutionPlan, PlanStep, RunMode, StepEvaluation, StrategySelection

from .base import PlanningError


class RuleBasedPlanner:
    name = "rule"

    def decide(
        self,
        history: list[dict[str, str]],
        mode: RunMode,
        on_reasoning: Callable[[str], None] | None = None,
    ) -> AgentAction:
        task = history[-1]["content"].removeprefix("[Tool result]\n")
        if history[-1]["content"].startswith("[Tool result]\n"):
            return AgentAction(type="final_answer", answer=task)
        task = task.strip()
        if not task:
            return AgentAction(type="final_answer", answer="Please provide a task.")
        if mode == "plan":
            return AgentAction(
                type="final_answer",
                answer=f"1. Inspect the relevant files for: {task}\n2. Identify the smallest safe change.\n3. Implement and test the change after leaving Plan mode.",
            )
        expression = self._expression(task)
        if expression:
            return AgentAction(type="tool_call", tool="calculator", arguments={"expression": expression})
        file_match = re.search(r"(?:read|show|读取|查看)\s+[`'\"]?([^`'\"\s]+)", task, flags=re.IGNORECASE)
        if file_match:
            path = file_match.group(1).rstrip("。.!！")
            return AgentAction(type="tool_call", tool="read_file", arguments={"path": path})
        if re.search(r"(?:list|files|目录|文件)", task, flags=re.IGNORECASE):
            return AgentAction(type="tool_call", tool="list_files", arguments={})
        command_match = re.search(r"(?:run|execute)\s+(?:command\s+)?(.+)$|执行命令\s+(.+)$", task, flags=re.IGNORECASE)
        if command_match:
            command = next(value for value in command_match.groups() if value is not None).strip()
            return AgentAction(type="tool_call", tool="run_command", arguments={"command": command})
        return AgentAction(type="final_answer", answer="Hello! I can help inspect files, calculate expressions, and plan safe changes.")

    def select_strategy(self, history: list[dict[str, str]], mode: RunMode) -> StrategySelection:
        """Keep offline demonstrations deterministic; LLMPlanner performs live routing."""
        return StrategySelection("reactive", "Offline rule planner uses its deterministic reactive loop.")

    def create_plan(
        self,
        history: list[dict[str, str]],
        mode: RunMode,
        on_reasoning: Callable[[str], None] | None = None,
    ) -> ExecutionPlan:
        """Provide a deterministic single-step execution plan for offline demos."""
        task_message = next(
            (item for item in reversed(history) if not item["content"].startswith("[Plan feedback]")),
            history[-1],
        )
        action = self.decide([task_message], "agent", on_reasoning=on_reasoning)
        if action.type == "final_answer":
            return ExecutionPlan(goal=task_message["content"], final_answer=action.answer)
        assert action.tool is not None
        return ExecutionPlan(
            goal=task_message["content"],
            steps=[
                PlanStep(
                    id="step_1",
                    description=f"Call {action.tool}",
                    action=action,
                    success_criteria="The tool call succeeds.",
                )
            ],
        )

    def create_dynamic_plan(
        self,
        history: list[dict[str, str]],
        mode: RunMode,
        on_reasoning: Callable[[str], None] | None = None,
    ) -> ExecutionPlan:
        return self.create_plan(history, mode, on_reasoning=on_reasoning)

    def evaluate_step(
        self,
        history: list[dict[str, str]],
        plan: ExecutionPlan,
        step: PlanStep,
        result: str,
    ) -> StepEvaluation:
        return StepEvaluation("continue", "Offline rule planner accepts successful tool results.")

    def replan(
        self,
        history: list[dict[str, str]],
        plan: ExecutionPlan,
        reason: str,
        on_reasoning: Callable[[str], None] | None = None,
    ) -> ExecutionPlan:
        raise PlanningError("Offline rule planner cannot repair a failed plan.")

    @staticmethod
    def _expression(task: str) -> str | None:
        candidate = re.search(r"(?:calculate|compute|计算)\s+(.+)$", task, flags=re.IGNORECASE)
        if candidate:
            return candidate.group(1).strip().rstrip("。.!！")
        if re.fullmatch(r"[0-9\s+\-*/%().]+", task):
            return task
        return None
