"""The composition root for a local-first Mini-Agent client."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Literal

from backend.configuration import ClientPaths, initialize_config, section
from backend.mcp.client import load_external_tools
from backend.planning import LLMPlanner, RuleBasedPlanner
from backend.providers import LLMClient, ModelConfig
from backend.skills import SkillCatalog
from backend.storage.sqlite import SQLiteSessionStore
from backend.sync import RequestsSyncTransport, SyncClient, SyncCoordinator
from backend.tools import ToolExecutor, WorkspaceFiles, build_tool_registry, delegation_tools

from ..conversation.references import FileReferenceExpander
from ..core.config import RunnerSettings, log_full_messages_from_toml
from ..core.hooks import AgentHook
from ..execution.runner import AgentRunner
from ..subagents import SubagentCoordinator
from .services import AgentApplication

PlannerName = Literal["llm", "rule"]


def client_paths() -> ClientPaths:
    return ClientPaths.from_home()


def build_session_store(workspace: Path, paths: ClientPaths | None = None) -> SQLiteSessionStore:
    resolved = paths or client_paths()
    config = initialize_config(resolved, workspace)
    return SQLiteSessionStore(resolved, str(section(config, "sync")["device_id"]))


def build_application(
    workspace: Path,
    planner_name: PlannerName = "llm",
    settings: RunnerSettings | None = None,
    hooks: Iterable[AgentHook] = (),
) -> AgentApplication:
    paths = client_paths()
    config = initialize_config(paths, workspace)
    resolved = _settings_for(workspace, paths, settings)
    store = build_session_store(workspace, paths)
    files = WorkspaceFiles(workspace)
    runner = _build_subagent_runner(workspace, planner_name, resolved, hooks, store, files, paths)
    sync_coordinator = _build_sync_coordinator(config, store)
    return AgentApplication(runner, store, FileReferenceExpander(files), sync_coordinator)


def build_runner(
    workspace: Path,
    planner_name: PlannerName = "llm",
    settings: RunnerSettings | None = None,
    hooks: Iterable[AgentHook] = (),
) -> AgentRunner:
    paths = client_paths()
    initialize_config(paths, workspace)
    return _build_subagent_runner(
        workspace, planner_name, _settings_for(workspace, paths, settings), hooks, paths=paths
    )


def _build_subagent_runner(
    workspace: Path,
    planner_name: PlannerName,
    settings: RunnerSettings,
    hooks: Iterable[AgentHook],
    checkpoints: object | None = None,
    files: WorkspaceFiles | None = None,
    paths: ClientPaths | None = None,
) -> AgentRunner:
    resolved_paths = paths or client_paths()

    def child_factory() -> AgentRunner:
        return _build_runner(
            workspace, planner_name, settings, build_tool_registry(workspace), hooks, checkpoints, resolved_paths
        )

    coordinator = SubagentCoordinator(child_factory)
    tools = build_tool_registry(
        workspace,
        workspace_files=files,
        extra_tools=(
            *delegation_tools(),
            *load_external_tools(resolved_paths.mcp_file, workspace / ".mini_agent" / "mcp.toml"),
        ),
    )
    return _build_runner(workspace, planner_name, settings, tools, hooks, checkpoints, resolved_paths, coordinator)


def _build_runner(
    workspace: Path,
    planner_name: PlannerName,
    settings: RunnerSettings,
    tools: ToolExecutor,
    hooks: Iterable[AgentHook],
    checkpoints: object | None,
    paths: ClientPaths,
    subagents: object | None = None,
) -> AgentRunner:
    skills = SkillCatalog.discover(workspace, global_root=paths.skills_dir)
    if planner_name == "rule":
        planner = RuleBasedPlanner()
    else:
        planner = LLMPlanner(
            LLMClient(ModelConfig.from_toml(paths.config_file)), tools.specs(), tools.read_only_specs()
        )
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
        checkpoints=checkpoints,
        hooks=hooks,
        skill_catalog=skills,
        workspace_root=str(workspace.resolve()),
        subagents=subagents,
    )


def _settings_for(workspace: Path, paths: ClientPaths, settings: RunnerSettings | None) -> RunnerSettings:
    return settings or RunnerSettings(log_full_messages=log_full_messages_from_toml(paths.config_file))


def _build_sync_coordinator(config: dict[str, object], store: SQLiteSessionStore) -> SyncCoordinator | None:
    sync = section(config, "sync")
    url = sync.get("url")
    token = sync.get("token")
    if url is None and token is None:
        return None
    if not isinstance(url, str) or not url or not isinstance(token, str) or not token:
        raise ValueError("sync.url and sync.token must be configured together.")
    device_id = str(sync["device_id"])
    coordinator = SyncCoordinator(
        SyncClient(device_id, RequestsSyncTransport(url, token, device_id)),
        store,
    )
    store.set_sync_listener(coordinator.notify)
    coordinator.start()
    return coordinator
