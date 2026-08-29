"""A transparent offline baseline planner."""

from __future__ import annotations

import re
from pathlib import Path

from backend.domain import (
    AssistantMessage,
    ToolMessage,
    UserMessage,
)
from backend.runtime.core.context import AgentRuntime


class RuleBasedPlanner:
    name = "rule"

    def finalize(self, runtime: AgentRuntime, reason: str) -> AssistantMessage:
        recent = ", ".join(f"{tool.name} ({tool.status})" for tool in runtime.run.actions[-3:])
        detail = f" Recent tool calls: {recent}." if recent else ""
        return AssistantMessage(
            content=(
                f"The run stopped because {reason} "
                f"It completed {len(runtime.run.actions)} tool calls before stopping.{detail} "
                "Continue the task in a new turn if more work is required."
            )
        )

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
            content="Hello! I can help with web search, file operations, and running commands in the workspace."
        )

    def _tool_for_task(self, task: str, runtime: AgentRuntime) -> ToolMessage | None:
        web = self._web_tool(task, runtime)
        if web is not None:
            return web
        file_match = re.search(r"(?:read|show|cat|查看|读取)\s+[`'\"]?([^`'\"\s]+)", task, flags=re.IGNORECASE)
        if file_match:
            path = file_match.group(1).rstrip("。.!！")
            candidate = Path(path)
            if not candidate.is_absolute():
                root = runtime.state.project_cwd or runtime.state.workspace_root
                if root:
                    path = str((Path(root) / candidate).resolve())
            return self._tool("read_file", {"path": path}, runtime)
        if re.search(r"(?:list|ls|files|目录|文件)", task, flags=re.IGNORECASE):
            return self._tool("glob", {"pattern": "**/*"}, runtime)
        command = re.search(r"(?:run|execute)\s+(?:command\s+)?(.+)$|执行命令\s+(.+)$", task, re.IGNORECASE)
        if command:
            value = next(group for group in command.groups() if group is not None).strip()
            return self._tool("run_command", {"command": value}, runtime)
        return None

    @staticmethod
    def _tool(name: str, arguments: dict[str, object], runtime: AgentRuntime) -> ToolMessage:
        return ToolMessage(name=name, call_id=runtime.next_tool_call_id(), arguments=arguments)

    def _web_tool(self, task: str, runtime: AgentRuntime) -> ToolMessage | None:
        search = re.search(r"(?:search|搜索)\s+(.+)$", task, re.IGNORECASE)
        if search:
            return self._tool("web_search", {"query": search.group(1).strip()}, runtime)
        fetch = re.search(r"(?:fetch|抓取(?:网页)?|访问)\s+(https?://\S+)", task, re.IGNORECASE)
        if fetch:
            return self._tool("web_fetch", {"url": fetch.group(1).rstrip("。.!！")}, runtime)
        return None
