"""LLM planner plans behavior."""

from __future__ import annotations

from backend.domain import (
    ExecutionPlan,
    ModelOutputError,
    PlanningError,
    StepEvaluation,
    SystemMessage,
    UserMessage,
)
from backend.runtime.core.context import AgentRuntime


class PlanMixin:
    def create_plan(self, runtime: AgentRuntime) -> ExecutionPlan:
        return self._with_output_repair(
            runtime,
            "plan",
            lambda correction: self._create_plan_once(runtime, correction),
        )

    def _create_plan_once(self, runtime: AgentRuntime, correction: UserMessage | None = None) -> ExecutionPlan:
        raw = self._json_request(
            runtime,
            self._plan_system(dynamic=False, allowed_specs=self.tool_specs),
            "plan",
            operation_tools=self.tool_specs,
            extra=[correction] if correction is not None else None,
        )
        return self._parse_execution_plan(raw, runtime, runtime.exchange.operation_tools)

    def create_dynamic_plan(self, runtime: AgentRuntime) -> ExecutionPlan:
        return self._with_output_repair(
            runtime,
            "plan",
            lambda correction: self._create_dynamic_plan_once(runtime, correction),
        )

    def _create_dynamic_plan_once(self, runtime: AgentRuntime, correction: UserMessage | None = None) -> ExecutionPlan:
        allowed = self.read_only_tool_specs if runtime.run.mode == "plan" else self.tool_specs
        raw = self._json_request(
            runtime,
            self._plan_system(dynamic=True, allowed_specs=allowed),
            "plan",
            extra=[correction] if correction is not None else None,
            operation_tools=allowed,
        )
        return self._parse_execution_plan(raw, runtime, runtime.exchange.operation_tools)

    def evaluate_step(self, runtime: AgentRuntime) -> StepEvaluation:
        return self._with_output_repair(
            runtime,
            "evaluate",
            lambda correction: self._evaluate_step_once(runtime, correction),
        )

    def _evaluate_step_once(self, runtime: AgentRuntime, correction: UserMessage | None = None) -> StepEvaluation:
        context = runtime.exchange.context
        plan = context.get("plan")
        step = context.get("step")
        result = context.get("result")
        if plan is None or step is None or not isinstance(result, str):
            raise PlanningError("Step evaluation context is incomplete.")
        prompt = UserMessage(
            content=(
                f"Plan goal: {plan.goal}\n"
                f"Step: {step.description}\n"
                f"Expected success criterion: {step.success_criteria}\n"
                f"Actual result: {result}\n\n"
                "Evaluate:\n"
                "- Did the step achieve its goal? Does the output match the success criteria?\n"
                "- Did the step reveal new information that changes the plan?\n"
                "- Are the remaining steps still necessary and correctly ordered?\n"
                "- Is the overall goal still achievable with the current approach?\n\n"
                'Return JSON only: {"decision":"continue|replan","reason":"text"}.'
            )
        )
        raw = self._json_request(
            runtime,
            SystemMessage(
                content=(
                    "You are evaluating a completed step in an execution plan. "
                    "Assess whether the step succeeded and whether the remaining plan "
                    "is still valid. Choose continue if the step met its goal and the "
                    "plan is still correct; choose replan if the result invalidates the "
                    "remaining steps or reveals that a different approach is needed."
                )
            ),
            "evaluate",
            extra=[prompt, *([correction] if correction is not None else [])],
        )
        payload = self._json_object(raw)
        decision = payload.get("decision")
        reason = payload.get("reason")
        if decision not in {"continue", "replan"}:
            raise ModelOutputError(
                "Step evaluation decision must be continue or replan.",
                operation="evaluate",
                invalid_output=raw,
            )
        if not isinstance(reason, str) or not reason.strip():
            raise ModelOutputError(
                "Step evaluation reason must be non-empty text.",
                operation="evaluate",
                invalid_output=raw,
            )
        return StepEvaluation(decision, reason.strip())

    def replan(self, runtime: AgentRuntime) -> ExecutionPlan:
        return self._with_output_repair(
            runtime,
            "replan",
            lambda correction: self._replan_once(runtime, correction),
        )

    def _replan_once(self, runtime: AgentRuntime, correction: UserMessage | None = None) -> ExecutionPlan:
        context = runtime.exchange.context
        plan = context.get("plan")
        reason = context.get("reason")
        if plan is None or not isinstance(reason, str):
            raise PlanningError("Replan context is incomplete.")
        extra = UserMessage(
            content=(
                f"Current plan: {self._plan_json(plan)}\nReason for replacement: {reason}\n"
                "Return a replacement plan for unfinished work only."
            )
        )
        raw = self._json_request(
            runtime,
            self._plan_system(dynamic=True, allowed_specs=self.tool_specs),
            "replan",
            extra=[extra, *([correction] if correction is not None else [])],
            operation_tools=self.tool_specs,
        )
        return self._parse_execution_plan(raw, runtime, runtime.exchange.operation_tools)
