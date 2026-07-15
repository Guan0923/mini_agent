"""The composition root: selects implementations without involving the TUI."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from mini_agent.planning import LLMPlanner, RuleBasedPlanner
from mini_agent.providers import DeepSeekChatCompletions, ModelConfig
from mini_agent.tools import ToolRegistry

from .checkpoints import SQLiteCheckpointStore
from .config import RunnerSettings
from .runner import AgentRunner

PlannerName = Literal["llm", "rule"]


def build_runner(
    workspace: Path,
    planner_name: PlannerName = "llm",
    settings: RunnerSettings | None = None,
) -> AgentRunner:
    settings = settings or RunnerSettings()
    tools = ToolRegistry(workspace)
    if planner_name == "rule":
        planner = RuleBasedPlanner()
    else:
        client = DeepSeekChatCompletions(ModelConfig.from_env(workspace / ".env"))
        planner = LLMPlanner(client, tools.names(), tools.read_only_names())
    return AgentRunner(
        planner=planner,
        tools=tools,
        max_retries=settings.max_retries,
        max_actions=settings.max_actions,
        max_replans=settings.max_replans,
        strategy=settings.strategy,
        checkpoints=SQLiteCheckpointStore(workspace / ".mini_agent" / "checkpoints.db"),
    )
