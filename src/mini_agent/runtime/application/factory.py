"""The composition root: selects implementations without involving the TUI."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Literal

from mini_agent.planning import LLMPlanner, RuleBasedPlanner
from mini_agent.providers import LLMClient, ModelConfig
from mini_agent.storage import SQLiteCheckpointStore, SQLiteSessionStore
from mini_agent.tools import ToolExecutor, WorkspaceFiles, build_tool_registry

from ..conversation.references import FileReferenceExpander
from ..core.config import RunnerSettings, log_full_messages_from_env
from ..core.hooks import AgentHook
from ..execution.runner import AgentRunner
from .services import AgentApplication

PlannerName = Literal["llm", "rule"]


def session_database_path(workspace: Path) -> Path:
    """Return the workspace-local database shared by checkpoints and sessions."""

    return workspace / ".mini_agent" / "checkpoints.db"


def build_session_store(workspace: Path) -> SQLiteSessionStore:
    """Construct the workspace-local session store."""

    return SQLiteSessionStore(session_database_path(workspace))


def build_application(
    workspace: Path,
    planner_name: PlannerName = "llm",
    settings: RunnerSettings | None = None,
    hooks: Iterable[AgentHook] = (),
) -> AgentApplication:
    """Compose one interface-neutral application with its workspace dependencies."""

    files = WorkspaceFiles(workspace)
    tools = build_tool_registry(workspace, workspace_files=files)
    runner = _build_runner(workspace, planner_name, _settings_for(workspace, settings), tools, hooks)
    return AgentApplication(runner, build_session_store(workspace), FileReferenceExpander(files))


def build_runner(
    workspace: Path,
    planner_name: PlannerName = "llm",
    settings: RunnerSettings | None = None,
    hooks: Iterable[AgentHook] = (),
) -> AgentRunner:
    return _build_runner(
        workspace,
        planner_name,
        _settings_for(workspace, settings),
        build_tool_registry(workspace),
        hooks,
    )


def _build_runner(
    workspace: Path,
    planner_name: PlannerName,
    settings: RunnerSettings,
    tools: ToolExecutor,
    hooks: Iterable[AgentHook],
) -> AgentRunner:
    if planner_name == "rule":
        planner = RuleBasedPlanner()
    else:
        client = LLMClient(ModelConfig.from_env(workspace / ".env"))
        planner = LLMPlanner(client, tools.specs(), tools.read_only_specs())
    return AgentRunner(
        planner=planner,
        tools=tools,
        max_model_repairs=settings.max_model_repairs,
        max_transport_retries=settings.max_transport_retries,
        max_retries=settings.max_retries,
        max_tool_recoveries=settings.max_tool_recoveries,
        max_actions=settings.max_actions,
        max_replans=settings.max_replans,
        strategy=settings.strategy,
        log_full_messages=settings.log_full_messages,
        checkpoints=SQLiteCheckpointStore(session_database_path(workspace)),
        hooks=hooks,
    )


def _settings_for(workspace: Path, settings: RunnerSettings | None) -> RunnerSettings:
    if settings is not None:
        return settings
    return RunnerSettings(log_full_messages=log_full_messages_from_env(workspace / ".env"))
