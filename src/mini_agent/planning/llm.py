"""LLM-backed planner with strict local validation of the returned plan."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import asdict
from typing import Protocol

from mini_agent.domain import AgentAction, ExecutionPlan, PlanStep, RunMode, StepEvaluation, StrategySelection
from mini_agent.providers import ModelRequestError

from .base import PlanningError


class CompletionClient(Protocol):
    def complete(self, messages: list[dict[str, str]]) -> str: ...


class LLMPlanner:
    name = "llm"

    def __init__(self, client: CompletionClient, tool_names: list[str], read_only_tool_names: list[str]) -> None:
        self.client = client
        self.tool_names = set(tool_names)
        self.read_only_tool_names = set(read_only_tool_names)

    def decide(
        self,
        history: list[dict[str, str]],
        mode: RunMode,
        on_reasoning: Callable[[str], None] | None = None,
    ) -> AgentAction:
        allowed_tools = self.read_only_tool_names if mode == "plan" else self.tool_names
        mode_instruction = (
            "You are in read-only Plan mode. You may call only the listed read-only tools to gather facts. "
            "Never call a write or modification tool. When ready, return a final_answer containing a concise numbered implementation plan; do not perform the plan."
            if mode == "plan"
            else "You are in Agent mode. Decide the next atomic action needed to help the user."
        )
        messages = [
            {
                "role": "system",
                "content": (
                    "You are the decision component of a local terminal agent. "
                    "Return JSON only. Choose exactly one schema: "
                    '{"type":"tool_call","tool":"tool name","arguments":{}} or '
                    '{"type":"final_answer","answer":"text"}. '
                    "Call at most one tool at a time; after each tool result you will decide again. "
                    "Never use shell commands or invent tools. "
                    f"Allowed tools: {', '.join(sorted(allowed_tools))}. "
                    + mode_instruction
                ),
            },
            *history,
        ]
        raw, reasoning = self._complete(messages, on_reasoning)
        action = self._parse_action(raw, allowed_tools)
        return AgentAction(
            type=action.type,
            tool=action.tool,
            arguments=action.arguments,
            answer=action.answer,
            reasoning=reasoning,
        )

    def select_strategy(self, history: list[dict[str, str]], mode: RunMode) -> StrategySelection:
        """Ask the model to classify the task before execution begins."""
        if mode == "plan":
            return StrategySelection("reactive", "Plan mode requires human approval before execution.")
        messages = [
            {
                "role": "system",
                "content": (
                    "You route tasks for a local terminal agent. Return JSON only using exactly "
                    '{"strategy":"reactive|plan_execute|dynamic_replan","reason":"short explanation"}. '
                    "Choose reactive for direct answers, simple requests, exploratory tasks, or tasks whose "
                    "next action is unclear. Choose plan_execute only when the task has multiple concrete "
                    "tool steps whose arguments can be planned before execution. Choose dynamic_replan for "
                    "multi-step tasks where tool failures or real tool output may invalidate remaining steps. "
                    "Available tools: "
                    f"{', '.join(sorted(self.tool_names))}."
                ),
            },
            *history,
        ]
        raw, _reasoning = self._complete(messages, None)
        return self._parse_strategy_selection(raw)

    def create_plan(
        self,
        history: list[dict[str, str]],
        mode: RunMode,
        on_reasoning: Callable[[str], None] | None = None,
    ) -> ExecutionPlan:
        """Create a complete, locally validated plan for plan_execute."""
        # Plan mode generates proposals only; execution still requires human approval.
        allowed_tools = self.tool_names
        messages = [
            {
                "role": "system",
                "content": (
                    "You are the planning component of a local terminal agent. Return JSON only. "
                    "Before execution, create a complete fixed plan using only the allowed tools. "
                    "Use this schema exactly: "
                    '{"goal":"text","steps":[{"id":"step_1","description":"text",'
                    '"success_criteria":"text","tool":"tool name","arguments":{}}]}. '
                    "Every step must be a concrete tool call with complete arguments; do not rely on later "
                    "tool results to fill arguments. A plan may use zero steps only for a direct response, "
                    "in which case include a non-empty top-level final_answer string. "
                    "If the conversation includes a [Plan feedback] message, revise the plan to satisfy that feedback. "
                    "Do not include final_answer when steps are present. Never use shell commands or invent tools. "
                    f"Allowed tools: {', '.join(sorted(allowed_tools))}."
                ),
            },
            *history,
        ]
        raw, _reasoning = self._complete(messages, on_reasoning)
        return self._parse_execution_plan(raw, allowed_tools)

    def create_dynamic_plan(
        self,
        history: list[dict[str, str]],
        mode: RunMode,
        on_reasoning: Callable[[str], None] | None = None,
    ) -> ExecutionPlan:
        """Create only the first concrete phase of a plan that may require later replanning."""
        allowed_tools = self.read_only_tool_names if mode == "plan" else self.tool_names
        messages = [
            {
                "role": "system",
                "content": (
                    "You create the first executable phase of a dynamic local terminal-agent plan. Return JSON only. "
                    "Use exactly this JSON schema: "
                    '{"goal":"text","steps":[{"id":"step_1","description":"text",'
                    '"success_criteria":"text","tool":"tool name","arguments":{}}]}. '
                    "Include only tool calls whose arguments are already known now. Do not invent file contents, "
                    "paths, or write arguments that depend on a future read result. Stop the phase after gathering "
                    "the facts needed for an uncertain next action; the runtime will request a replacement phase. "
                    "An empty plan is allowed only with a non-empty top-level final_answer. Never use shell commands "
                    f"or invent tools. Allowed tools: {', '.join(sorted(allowed_tools))}."
                ),
            },
            *history,
        ]
        raw, _reasoning = self._complete(messages, on_reasoning)
        return self._parse_execution_plan(raw, allowed_tools)

    def evaluate_step(
        self,
        history: list[dict[str, str]],
        plan: ExecutionPlan,
        step: PlanStep,
        result: str,
    ) -> StepEvaluation:
        """Decide whether a successful step leaves the remaining plan valid."""
        remaining = [candidate.description for candidate in plan.steps if candidate.status == "pending"]
        messages = [
            {
                "role": "system",
                "content": (
                    "You evaluate one completed step of a fixed agent plan. Return JSON only using exactly "
                    '{"decision":"continue|replan","reason":"short explanation"}. '
                    "Choose continue only when the result satisfies the step and the active plan can still complete "
                    "the goal. Choose replan when the result makes remaining steps invalid or unsafe, or when the "
                    "current phase has no remaining steps but the overall goal is not yet complete."
                ),
            },
            *history,
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "goal": plan.goal,
                        "completed_step": asdict(step),
                        "result": result,
                        "remaining_steps": remaining,
                    },
                    ensure_ascii=False,
                ),
            },
        ]
        raw, _reasoning = self._complete(messages, None)
        return self._parse_step_evaluation(raw)

    def replan(
        self,
        history: list[dict[str, str]],
        plan: ExecutionPlan,
        reason: str,
        on_reasoning: Callable[[str], None] | None = None,
    ) -> ExecutionPlan:
        """Replace only unfinished work after the active plan has failed or deviated."""
        messages = [
            {
                "role": "system",
                "content": (
                    "You repair the remaining work of a local terminal agent plan. Return JSON only. "
                    "Create a replacement plan containing only uncompleted work; do not repeat completed steps. "
                    "Use exactly this schema: "
                    '{"goal":"text","steps":[{"id":"step_1","description":"text",'
                    '"success_criteria":"text","tool":"tool name","arguments":{}}]}. '
                    "Every step must be a concrete tool call with complete arguments. An empty plan is allowed "
                    "only with a non-empty top-level final_answer. Never use shell commands or invent tools. "
                    f"Allowed tools: {', '.join(sorted(self.tool_names))}."
                ),
            },
            *history,
            {
                "role": "user",
                "content": json.dumps(
                    {"active_plan": asdict(plan), "replan_reason": reason}, ensure_ascii=False
                ),
            },
        ]
        raw, _reasoning = self._complete(messages, on_reasoning)
        return self._parse_execution_plan(raw, self.tool_names)

    def _complete(
        self,
        messages: list[dict[str, str]],
        on_reasoning: Callable[[str], None] | None,
    ) -> tuple[str, str | None]:
        """Use provider reasoning metadata when the configured client exposes it."""
        stream_with_reasoning = getattr(self.client, "stream_with_reasoning", None)
        if on_reasoning is not None and callable(stream_with_reasoning):
            content_chunks: list[str] = []
            reasoning_chunks: list[str] = []
            for delta in stream_with_reasoning(messages):
                if delta.reasoning_content:
                    reasoning_chunks.append(delta.reasoning_content)
                    on_reasoning(delta.reasoning_content)
                if delta.content:
                    content_chunks.append(delta.content)
                if delta.finish_reason == "length":
                    raise ModelRequestError("DeepSeek stopped the JSON response because max_tokens was reached.")
            content = "".join(content_chunks)
            if not content.strip():
                raise ModelRequestError("DeepSeek JSON mode returned empty content; retry with a clearer JSON prompt.")
            return content, "".join(reasoning_chunks) or None
        complete_with_reasoning = getattr(self.client, "complete_with_reasoning", None)
        if callable(complete_with_reasoning):
            return complete_with_reasoning(messages)
        return self.client.complete(messages), None

    def _parse_action(self, raw: str, allowed_tools: set[str]) -> AgentAction:
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.IGNORECASE)
        try:
            payload = json.loads(cleaned)
            action_type = payload["type"]
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise PlanningError("Model did not return the required action JSON.") from exc
        if action_type == "tool_call":
            tool = payload.get("tool")
            arguments = payload.get("arguments", {})
            if tool not in allowed_tools:
                raise PlanningError(f"Model requested unavailable tool: {tool!r}.")
            if not isinstance(arguments, dict):
                raise PlanningError("Tool action arguments must be an object.")
            return AgentAction(type="tool_call", tool=tool, arguments=arguments)
        if action_type == "final_answer":
            answer = payload.get("answer")
            if not isinstance(answer, str) or not answer.strip():
                raise PlanningError("Final answer action must contain text.")
            return AgentAction(type="final_answer", answer=answer.strip())
        raise PlanningError(f"Unsupported action type: {action_type!r}.")

    def _parse_execution_plan(self, raw: str, allowed_tools: set[str]) -> ExecutionPlan:
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.IGNORECASE)
        try:
            payload = json.loads(cleaned)
            goal = payload["goal"]
            steps = payload["steps"]
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise PlanningError("Model did not return the required execution-plan JSON.") from exc
        if not isinstance(goal, str) or not goal.strip():
            raise PlanningError("Execution plan goal must contain text.")
        if not isinstance(steps, list):
            raise PlanningError("Execution plan steps must be an array.")

        parsed_steps: list[PlanStep] = []
        seen_ids: set[str] = set()
        for raw_step in steps:
            if not isinstance(raw_step, dict):
                raise PlanningError("Each execution-plan step must be an object.")
            step_id = raw_step.get("id")
            description = raw_step.get("description")
            success_criteria = raw_step.get("success_criteria", "")
            tool = raw_step.get("tool")
            arguments = raw_step.get("arguments", {})
            if not isinstance(step_id, str) or not step_id.strip() or step_id in seen_ids:
                raise PlanningError("Execution-plan step ids must be unique non-empty strings.")
            if not isinstance(description, str) or not description.strip():
                raise PlanningError("Execution-plan steps need a description.")
            if not isinstance(success_criteria, str):
                raise PlanningError("Execution-plan success criteria must be text.")
            if tool not in allowed_tools:
                raise PlanningError(f"Model planned unavailable tool: {tool!r}.")
            if not isinstance(arguments, dict):
                raise PlanningError("Execution-plan tool arguments must be an object.")
            seen_ids.add(step_id)
            parsed_steps.append(
                PlanStep(
                    id=step_id,
                    description=description.strip(),
                    success_criteria=success_criteria.strip(),
                    action=AgentAction(type="tool_call", tool=tool, arguments=arguments),
                )
            )

        final_answer = payload.get("final_answer")
        if parsed_steps and final_answer is not None:
            raise PlanningError("Execution plans with steps must not include final_answer.")
        if not parsed_steps:
            if not isinstance(final_answer, str) or not final_answer.strip():
                raise PlanningError("An empty execution plan needs a non-empty final_answer.")
            final_answer = final_answer.strip()
        return ExecutionPlan(goal=goal.strip(), steps=parsed_steps, final_answer=final_answer)

    @staticmethod
    def _parse_strategy_selection(raw: str) -> StrategySelection:
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.IGNORECASE)
        try:
            payload = json.loads(cleaned)
            strategy = payload["strategy"]
            reason = payload["reason"]
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise PlanningError("Model did not return the required strategy-selection JSON.") from exc
        if strategy not in {"reactive", "plan_execute", "dynamic_replan"}:
            raise PlanningError(f"Unsupported execution strategy: {strategy!r}.")
        if not isinstance(reason, str) or not reason.strip():
            raise PlanningError("Strategy-selection reason must contain text.")
        return StrategySelection(strategy=strategy, reason=reason.strip())

    @staticmethod
    def _parse_step_evaluation(raw: str) -> StepEvaluation:
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.IGNORECASE)
        try:
            payload = json.loads(cleaned)
            decision = payload["decision"]
            reason = payload["reason"]
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise PlanningError("Model did not return the required step-evaluation JSON.") from exc
        if decision not in {"continue", "replan"}:
            raise PlanningError(f"Unsupported step-evaluation decision: {decision!r}.")
        if not isinstance(reason, str) or not reason.strip():
            raise PlanningError("Step-evaluation reason must contain text.")
        return StepEvaluation(decision=decision, reason=reason.strip())
