"""The composition root for a local-first Mini-Agent client."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Literal

from backend.configuration import ClientPaths, UserConfigStore, initialize_config, load_config, section
from backend.domain import DEFAULT_TIME_ZONE
from backend.mcp.client import ExternalMcpResources, start_external_tools
from backend.mcp.config import McpSettings, prepare_mcp_plan
from backend.planning import LLMPlanner, RuleBasedPlanner
from backend.providers import LLMClient, ModelConfig
from backend.skills import ProjectSkillGate, ProjectSkillTrustStore, SkillCatalog
from backend.storage.sqlite import SQLiteSessionStore
from backend.sync import RequestsSyncTransport, SyncClient, SyncCoordinator
from backend.tools import ToolExecutor, WorkspaceFiles, build_tool_registry, delegation_tools

from ..capability_settings import SkillSettings, SubagentSettings
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
    *,
    paths: ClientPaths | None = None,
    user_preferences: str = "",
    model_config: ModelConfig | None = None,
    config_override: dict[str, object] | None = None,
    default_timezone: str = DEFAULT_TIME_ZONE,
    session_provisioner: object | None = None,
    session_provisioner_cleanup: object | None = None,
    project_id: str | None = None,
) -> AgentApplication:
    resolved_paths = paths or client_paths()
    base_config = initialize_config(resolved_paths, workspace)
    config = dict(base_config)
    if config_override is not None:
        for name, value in config_override.items():
            if isinstance(value, dict) and isinstance(config.get(name), dict):
                config[name] = {**config[name], **value}
            else:
                config[name] = value
    resolved = _settings_for(resolved_paths, settings, config_override)
    device_id = str(section(config, "sync").get("device_id") or f"local_{resolved_paths.root.name}")
    store = SQLiteSessionStore(resolved_paths, device_id)
    files = WorkspaceFiles(workspace)
    runner_args = (
        workspace,
        planner_name,
        resolved,
        hooks,
        config,
        store,
        files,
        resolved_paths,
        user_preferences,
    )
    if model_config is None:
        runner = _build_subagent_runner(*runner_args, **({"project_id": project_id} if project_id else {}))
    else:
        runner = _build_subagent_runner(
            *runner_args,
            model_config=model_config,
            **({"project_id": project_id} if project_id else {}),
        )
    try:
        sync_coordinator = _build_sync_coordinator(config, store)
    except Exception:
        runner.close()
        raise
    return AgentApplication(
        runner,
        store,
        FileReferenceExpander(files),
        sync_coordinator,
        default_timezone,
        session_provisioner,
        session_provisioner_cleanup,
        project_id=project_id,
    )


def build_runner(
    workspace: Path,
    planner_name: PlannerName = "llm",
    settings: RunnerSettings | None = None,
    hooks: Iterable[AgentHook] = (),
    user_preferences: str = "",
    model_config: ModelConfig | None = None,
) -> AgentRunner:
    paths = client_paths()
    config = initialize_config(paths, workspace)
    return _build_subagent_runner(
        workspace,
        planner_name,
        _settings_for(paths, settings),
        hooks,
        config,
        paths=paths,
        user_preferences=user_preferences,
        model_config=model_config,
    )


def _build_subagent_runner(
    workspace: Path,
    planner_name: PlannerName,
    settings: RunnerSettings,
    hooks: Iterable[AgentHook],
    config: dict[str, object],
    checkpoints: object | None = None,
    files: WorkspaceFiles | None = None,
    paths: ClientPaths | None = None,
    user_preferences: str = "",
    *,
    model_config: ModelConfig | None = None,
    project_id: str | None = None,
) -> AgentRunner:
    resolved_paths = paths or client_paths()
    skill_settings = SkillSettings.from_config(config)
    subagent_settings = SubagentSettings.from_config(config)

    def child_factory() -> AgentRunner:
        return _build_runner(
            workspace,
            planner_name,
            settings,
            build_tool_registry(workspace),
            hooks,
            checkpoints,
            resolved_paths,
            skill_settings,
            user_preferences=user_preferences,
            model_config=model_config,
            project_id=project_id,
        )

    coordinator = SubagentCoordinator(child_factory, workspace, subagent_settings)
    external = _external_resources(workspace, resolved_paths, config)
    try:
        tools = build_tool_registry(
            workspace,
            workspace_files=files,
            extra_tools=(
                *delegation_tools(subagent_settings.max_tasks_per_batch),
                *external,
            ),
        )
        return _build_runner(
            workspace,
            planner_name,
            settings,
            tools,
            hooks,
            checkpoints,
            resolved_paths,
            skill_settings,
            coordinator,
            resources=(external,),
            user_preferences=user_preferences,
            model_config=model_config,
            project_id=project_id,
        )
    except Exception:
        external.close()
        raise


def _build_runner(
    workspace: Path,
    planner_name: PlannerName,
    settings: RunnerSettings,
    tools: ToolExecutor,
    hooks: Iterable[AgentHook],
    checkpoints: object | None,
    paths: ClientPaths,
    skill_settings: SkillSettings,
    subagents: object | None = None,
    *,
    resources: tuple[object, ...] = (),
    user_preferences: str = "",
    model_config: ModelConfig | None = None,
    project_id: str | None = None,
) -> AgentRunner:
    skills = SkillCatalog.discover(global_root=paths.skills_dir)
    project_skill_gate = (
        ProjectSkillGate(
            workspace,
            project_id,
            ProjectSkillTrustStore(UserConfigStore(paths.config_file)),
        )
        if project_id
        else None
    )
    if planner_name == "rule":
        planner = RuleBasedPlanner()
    else:
        planner = LLMPlanner(
            LLMClient(model_config or ModelConfig.from_toml(paths.config_file)),
            tools.specs(),
            tools.read_only_specs(),
            user_preferences=user_preferences,
        )
    return AgentRunner(
        planner=planner,
        tools=tools,
        max_transport_retries=settings.max_transport_retries,
        max_tool_calls=settings.max_tool_calls,
        log_full_messages=settings.log_full_messages,
        checkpoints=checkpoints,
        hooks=hooks,
        skill_catalog=skills,
        skill_auto_select=skill_settings.auto_select,
        project_skill_gate=project_skill_gate,
        workspace_root=str(workspace.resolve()),
        subagents=subagents,
        resources=resources,
    )


def _external_resources(
    workspace: Path,
    paths: ClientPaths,
    config: dict[str, object],
) -> ExternalMcpResources:
    del workspace
    plan = prepare_mcp_plan(paths)
    return start_external_tools(
        plan.effective_servers(),
        McpSettings.from_config(config),
    )


def _settings_for(
    paths: ClientPaths,
    settings: RunnerSettings | None,
    config_override: dict[str, object] | None = None,
) -> RunnerSettings:
    if settings is not None:
        return settings
    if config_override is not None:
        runtime = config_override.get("runtime")
        runtime_values = runtime if isinstance(runtime, dict) else {}
        max_tool_calls = runtime_values.get("max_tool_calls", 32)
        return RunnerSettings(
            max_tool_calls=max_tool_calls,  # type: ignore[arg-type]
            log_full_messages=bool(runtime_values.get("log_full_messages", True)),
        )
    config = load_config(paths.config_file)
    runtime = section(config, "runtime")
    max_tool_calls = runtime.get("max_tool_calls", 32)
    return RunnerSettings(
        max_tool_calls=max_tool_calls,  # type: ignore[arg-type]
        log_full_messages=log_full_messages_from_toml(paths.config_file),
    )


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
