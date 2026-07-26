"""The composition root: selects implementations without involving the TUI."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Literal

from backend.planning import LLMPlanner, RuleBasedPlanner
from backend.providers import LLMClient, ModelConfig
from backend.skills import SkillCatalog
from backend.storage import PostgresCheckpointStore, PostgresDatabase, PostgresSessionStore
from backend.tools import ToolExecutor, WorkspaceFiles, build_tool_registry, delegation_tools

from ..conversation.references import FileReferenceExpander
from ..core.config import RunnerSettings, database_url_from_env, log_full_messages_from_env
from ..core.hooks import AgentHook
from ..execution.runner import AgentRunner
from ..subagents import SubagentCoordinator
from .services import AgentApplication

PlannerName = Literal["llm", "rule"]


def database_url(workspace: Path) -> str:
    """Return the required PostgreSQL URL for workspace runtime storage."""

    return database_url_from_env(workspace / ".env")


def build_session_store(workspace: Path) -> PostgresSessionStore:
    """Construct the PostgreSQL session store."""

    return PostgresSessionStore(database_url(workspace))


def build_application(
    workspace: Path,
    planner_name: PlannerName = "llm",
    settings: RunnerSettings | None = None,
    hooks: Iterable[AgentHook] = (),
) -> AgentApplication:
    """Compose one interface-neutral application with its workspace dependencies."""

    files = WorkspaceFiles(workspace)
    resolved = _settings_for(workspace, settings)
    database = PostgresDatabase(database_url(workspace))
    session_store = PostgresSessionStore(database=database)
    runner = _build_subagent_runner(workspace, planner_name, resolved, hooks, session_store, files)
    return AgentApplication(runner, session_store, FileReferenceExpander(files), database)


def build_runner(
    workspace: Path,
    planner_name: PlannerName = "llm",
    settings: RunnerSettings | None = None,
    hooks: Iterable[AgentHook] = (),
) -> AgentRunner:
    resolved = _settings_for(workspace, settings)
    return _build_subagent_runner(workspace, planner_name, resolved, hooks)


def _build_subagent_runner(
    workspace: Path,
    planner_name: PlannerName,
    settings: RunnerSettings,
    hooks: Iterable[AgentHook],
    checkpoints: object | None = None,
    files: WorkspaceFiles | None = None,
) -> AgentRunner:
    def child_factory() -> AgentRunner:
        return _build_runner(
            workspace,
            planner_name,
            settings,
            build_tool_registry(workspace),
            hooks,
            checkpoints=checkpoints,
        )

    coordinator = SubagentCoordinator(child_factory)
    tools = build_tool_registry(workspace, workspace_files=files, extra_tools=delegation_tools())
    return _build_runner(
        workspace, planner_name, settings, tools, hooks, checkpoints=checkpoints, subagents=coordinator
    )


def _build_runner(
    workspace: Path,
    planner_name: PlannerName,
    settings: RunnerSettings,
    tools: ToolExecutor,
    hooks: Iterable[AgentHook],
    checkpoints: object | None = None,
    subagents: object | None = None,
) -> AgentRunner:
    skills = SkillCatalog.discover(workspace)
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
        max_model_turns=settings.max_model_turns,
        max_tool_calls=settings.max_tool_calls,
        max_replans=settings.max_replans,
        strategy=settings.strategy,
        log_full_messages=settings.log_full_messages,
        checkpoints=checkpoints or PostgresCheckpointStore(database_url(workspace)),
        hooks=hooks,
        skill_catalog=skills,
        workspace_root=str(workspace.resolve()),
        subagents=subagents,
    )


def _settings_for(workspace: Path, settings: RunnerSettings | None) -> RunnerSettings:
    if settings is not None:
        return settings
    return RunnerSettings(log_full_messages=log_full_messages_from_env(workspace / ".env"))
