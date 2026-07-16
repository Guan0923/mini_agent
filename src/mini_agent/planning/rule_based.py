"""A transparent offline baseline planner."""

from __future__ import annotations

import re

from mini_agent.domain import (
    AssistantMessage,
    ExecutionPlan,
    PlanningError,
    PlanStep,
    StepEvaluation,
    StrategySelection,
    ToolMessage,
    UserMessage,
)
from mini_agent.runtime.context import AgentRuntime


class RuleBasedPlanner:
    name = "rule"

    def decide(self, runtime: AgentRuntime) -> AssistantMessage:
        run = runtime.run
        last = runtime.state.messages[-1] if runtime.state.messages else UserMessage(content=run.task)
        if isinstance(last, AssistantMessage) and last.tool_messages:
            result = last.tool_messages[-1].content or ""
            return AssistantMessage(content=result.removeprefix("[Tool result]\n"))
        task = (last.content or "").strip()
        if not task:
            return AssistantMessage(content="Please provide a task.")
        if run.mode == "plan":
            return AssistantMessage(
                content=(
                    f"1. Inspect the relevant files for: {task}\n"
                    "2. Identify the smallest safe change.\n"
                    "3. Implement and test the change after leaving Plan mode."
                )
            )
        tool = self._tool_for_task(task, runtime)
        if tool is not None:
            return AssistantMessage(tool_messages=[tool])
        return AssistantMessage(
            content="Hello! I can help inspect, move, and delete workspace files; calculate expressions; and plan safe changes."
        )

    def select_strategy(self, runtime: AgentRuntime) -> StrategySelection:
        return StrategySelection("reactive", "Offline rule planner uses its deterministic reactive loop.")

    def create_plan(self, runtime: AgentRuntime) -> ExecutionPlan:
        task_message = next(
            (message for message in reversed(runtime.state.messages) if isinstance(message, UserMessage)),
            UserMessage(content=runtime.run.task),
        )
        original = runtime.state.messages
        runtime.state.messages = [task_message]
        try:
            response = self.decide(runtime)
        finally:
            runtime.state.messages = original
        if not response.tool_messages:
            return ExecutionPlan(goal=task_message.content or "", final_answer=response.content)
        return ExecutionPlan(
            goal=task_message.content or "",
            steps=[
                PlanStep(
                    id="step_1",
                    description=f"Call {response.tool_messages[0].name}",
                    tool_message=response.tool_messages[0],
                    success_criteria="The tool call succeeds.",
                )
            ],
        )

    def create_dynamic_plan(self, runtime: AgentRuntime) -> ExecutionPlan:
        return self.create_plan(runtime)

    def evaluate_step(self, runtime: AgentRuntime) -> StepEvaluation:
        return StepEvaluation("continue", "Offline rule planner accepts successful tool results.")

    def replan(self, runtime: AgentRuntime) -> ExecutionPlan:
        raise PlanningError("Offline rule planner cannot repair a failed plan.")

    def _tool_for_task(self, task: str, runtime: AgentRuntime) -> ToolMessage | None:
        web = self._web_tool(task, runtime)
        if web is not None:
            return web
        mutation = self._file_mutation(task, runtime)
        if mutation is not None:
            return mutation
        expression = self._expression(task)
        if expression:
            return self._tool("calculator", {"expression": expression}, runtime)
        file_match = re.search(r"(?:read|show|读取|查看)\s+[`'\"]?([^`'\"\s]+)", task, flags=re.IGNORECASE)
        if file_match:
            return self._tool("read_file", {"path": file_match.group(1).rstrip("。.!！")}, runtime)
        if re.search(r"(?:list|files|目录|文件)", task, flags=re.IGNORECASE):
            return self._tool("list_files", {}, runtime)
        command = re.search(r"(?:run|execute)\s+(?:command\s+)?(.+)$|执行命令\s+(.+)$", task, re.IGNORECASE)
        if command:
            value = next(group for group in command.groups() if group is not None).strip()
            return self._tool("run_command", {"command": value}, runtime)
        return None

    @staticmethod
    def _tool(name: str, arguments: dict[str, object], runtime: AgentRuntime) -> ToolMessage:
        return ToolMessage(name=name, call_id=runtime.next_tool_call_id(), arguments=arguments)

    @staticmethod
    def _expression(task: str) -> str | None:
        candidate = re.search(r"(?:calculate|compute|计算)\s+(.+)$", task, flags=re.IGNORECASE)
        if candidate:
            return candidate.group(1).strip().rstrip("。.!！")
        return task if re.fullmatch(r"[0-9\s+\-*/%().]+", task) else None

    def _web_tool(self, task: str, runtime: AgentRuntime) -> ToolMessage | None:
        search = re.search(r"(?:search|搜索)\s+(.+)$", task, re.IGNORECASE)
        if search:
            return self._tool("web_search", {"query": search.group(1).strip()}, runtime)
        fetch = re.search(r"(?:fetch|抓取(?:网页)?|访问)\s+(https?://\S+)", task, re.IGNORECASE)
        if fetch:
            return self._tool("web_fetch", {"url": fetch.group(1).rstrip("。.!！")}, runtime)
        return None

    def _file_mutation(self, task: str, runtime: AgentRuntime) -> ToolMessage | None:
        write = re.search(r"(?:write|写入)\s+([^\s]+)\s+(.+)$", task, re.IGNORECASE)
        if write:
            return self._tool("write_file", {"path": write.group(1), "content": write.group(2)}, runtime)
        delete_folder = re.search(
            r"(?:delete\s+folder\s+(recursive\s+)?|(?:递归)?删除(?:文件夹|目录)\s+)([^\s]+)",
            task,
            re.IGNORECASE,
        )
        if delete_folder:
            return self._tool(
                "delete_folder",
                {
                    "path": delete_folder.group(2),
                    "recursive": bool(delete_folder.group(1)) or task.startswith("递归"),
                },
                runtime,
            )
        delete = re.search(r"(?:delete\s+(?:file\s+)?|删除文件\s+)([^\s]+)", task, re.IGNORECASE)
        if delete:
            return self._tool("delete_file", {"path": delete.group(1)}, runtime)
        move_folder = re.search(r"(?:move\s+folder|移动文件夹)\s+([^\s]+)\s+(?:to|到)?\s*([^\s]+)", task, re.IGNORECASE)
        if move_folder:
            return self._tool(
                "move_folder",
                {"source": move_folder.group(1), "destination": move_folder.group(2)},
                runtime,
            )
        move = re.search(
            r"(?:move\s+(?:file\s+)?|移动文件\s+)([^\s]+)\s+(?:to|到)?\s*([^\s]+)",
            task,
            re.IGNORECASE,
        )
        if move:
            return self._tool("move_file", {"source": move.group(1), "destination": move.group(2)}, runtime)
        return None
