"""The composition root for a local-first Mini-Agent client."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from backend.configuration import ClientPaths, LocalConfigStore, initialize_config, load_config, section
from backend.domain import DEFAULT_TIME_ZONE
from backend.domain.terminal import DEFAULT_TERMINAL_TYPE
from backend.jobs import JobRegistry, JobScope, JobScopeKind
from backend.mcp.client import ExternalMcpResources, start_external_tools
from backend.mcp.config import McpSettings, prepare_mcp_plan
from backend.planning import LLMPlanner, RuleBasedPlanner
from backend.providers import LLMClient, ModelConfig
from backend.sandbox import (
    SandboxAdmission,
    SandboxLauncher,
    WindowsBrokerClient,
)
from backend.skills import ProjectSkillGate, ProjectSkillTrustStore, SkillCatalog
from backend.storage.settings import normalize_sandbox_config
from backend.storage.sqlite import SQLiteSessionStore
from backend.tools import ToolExecutor, WorkspaceFiles, build_tool_registry, delegation_tools
from backend.tools.terminal import effective_terminal_type

from ..capability_settings import McpCapabilitySettings, SkillSettings, SubagentSettings
from ..core.config import RunnerSettings, log_full_messages_from_toml
from ..execution.runner import AgentRunner
from ..subagents import SubagentCoordinator
from .services import AgentApplication

PlannerName = Literal["llm", "rule"]


def client_paths() -> ClientPaths:
    return ClientPaths.from_home()


def build_session_store(workspace: Path, paths: ClientPaths | None = None) -> SQLiteSessionStore:
    resolved = paths or client_paths()
    initialize_config(resolved, workspace)
    return SQLiteSessionStore(resolved)


def build_application(
    workspace: Path,
    planner_name: PlannerName = "llm",
    settings: RunnerSettings | None = None,
    *,
    paths: ClientPaths | None = None,
    user_preferences: str = "",
    model_config: ModelConfig | None = None,
    config_override: dict[str, object] | None = None,
    default_timezone: str = DEFAULT_TIME_ZONE,
    session_provisioner: object | None = None,
    session_provisioner_cleanup: object | None = None,
    project_id: str | None = None,
    project_cwd: Path | None = None,
    job_registry: JobRegistry | None = None,
    job_parent_id: str | None = None,
    sandbox_session_id: str | None = None,
    agent_thread_index: object | None = None,
    subagent_coordinator: SubagentCoordinator | None = None,
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
    store = SQLiteSessionStore(resolved_paths, agent_thread_index)
    skill_settings = SkillSettings.from_config(config)
    read_file_roots = _user_skill_read_roots(resolved_paths, skill_settings)
    files = WorkspaceFiles(workspace, project_workspace=project_cwd, read_file_roots=read_file_roots)
    runner_args = (
        workspace,
        planner_name,
        resolved,
        config,
        store,
        files,
        resolved_paths,
        user_preferences,
    )
    if model_config is None:
        runner = _build_subagent_runner(
            *runner_args,
            **({"project_id": project_id} if project_id else {}),
            **({"project_cwd": project_cwd} if project_cwd is not None else {}),
            **({"job_registry": job_registry} if job_registry is not None else {}),
            **({"job_parent_id": job_parent_id} if job_parent_id is not None else {}),
            **({"sandbox_session_id": sandbox_session_id} if sandbox_session_id is not None else {}),
            **({"subagent_coordinator": subagent_coordinator} if subagent_coordinator is not None else {}),
        )
    else:
        runner = _build_subagent_runner(
            *runner_args,
            model_config=model_config,
            **({"project_id": project_id} if project_id else {}),
            **({"project_cwd": project_cwd} if project_cwd is not None else {}),
            **({"job_registry": job_registry} if job_registry is not None else {}),
            **({"job_parent_id": job_parent_id} if job_parent_id is not None else {}),
            **({"sandbox_session_id": sandbox_session_id} if sandbox_session_id is not None else {}),
            **({"subagent_coordinator": subagent_coordinator} if subagent_coordinator is not None else {}),
        )
    return AgentApplication(
        runner,
        store,
        default_timezone,
        session_provisioner,
        session_provisioner_cleanup,
        project_id=project_id,
    )


def build_runner(
    workspace: Path,
    planner_name: PlannerName = "llm",
    settings: RunnerSettings | None = None,
    user_preferences: str = "",
    model_config: ModelConfig | None = None,
) -> AgentRunner:
    paths = client_paths()
    config = initialize_config(paths, workspace)
    return _build_subagent_runner(
        workspace,
        planner_name,
        _settings_for(paths, settings),
        config,
        paths=paths,
        user_preferences=user_preferences,
        model_config=model_config,
    )


def _build_subagent_runner(
    workspace: Path,
    planner_name: PlannerName,
    settings: RunnerSettings,
    config: dict[str, object],
    checkpoints: object | None = None,
    files: WorkspaceFiles | None = None,
    paths: ClientPaths | None = None,
    user_preferences: str = "",
    *,
    model_config: ModelConfig | None = None,
    project_id: str | None = None,
    project_cwd: Path | None = None,
    job_registry: JobRegistry | None = None,
    job_parent_id: str | None = None,
    terminal_type: str | None = None,
    sandbox_session_id: str | None = None,
    subagent_coordinator: SubagentCoordinator | None = None,
) -> AgentRunner:
    terminal_type = terminal_type or _terminal_type_for_config(config)
    resolved_paths = paths or client_paths()
    skill_settings = SkillSettings.from_config(config)
    subagent_settings = SubagentSettings.from_config(config)
    sandbox_launcher, sandbox_config = _sandbox_runtime(config, paths=resolved_paths)
    read_file_roots = _user_skill_read_roots(resolved_paths, skill_settings)
    workspace_files = files or WorkspaceFiles(
        workspace,
        project_workspace=project_cwd,
        read_file_roots=read_file_roots,
    )

    coordinator = subagent_coordinator or SubagentCoordinator(
        settings=subagent_settings,
        store=checkpoints,
        job_registry=job_registry,
    )

    def child_factory() -> AgentRunner:
        return _build_runner(
            workspace,
            planner_name,
            settings,
            build_tool_registry(
                workspace,
                workspace_files=WorkspaceFiles(
                    workspace,
                    project_workspace=project_cwd,
                    read_file_roots=read_file_roots,
                ),
                project_workspace=project_cwd,
                terminal_type=terminal_type,
                sandbox_config=sandbox_config,
                extra_tools=delegation_tools(subagent_settings.max_tasks_per_batch),
            ),
            checkpoints,
            resolved_paths,
            skill_settings,
            coordinator,
            user_preferences=user_preferences,
            model_config=model_config,
            project_id=project_id,
            project_cwd=project_cwd,
            job_registry=job_registry,
            job_parent_id=job_parent_id,
            sandbox_launcher=sandbox_launcher,
            sandbox_config=sandbox_config,
        )

    mcp_scope: JobScope | None = None
    if job_registry is not None:
        mcp_scope = job_registry.root_scope().child(JobScopeKind.SESSION, session_id=sandbox_session_id)
    external = _external_resources(
        resolved_paths,
        config,
        job_registry=job_registry,
        job_scope=mcp_scope,
    )
    try:
        tools = build_tool_registry(
            workspace,
            workspace_files=workspace_files,
            project_workspace=project_cwd,
            terminal_type=terminal_type,
            sandbox_config=sandbox_config,
            extra_tools=(
                *delegation_tools(subagent_settings.max_tasks_per_batch),
                *external,
            ),
        )
        runner = _build_runner(
            workspace,
            planner_name,
            settings,
            tools,
            checkpoints,
            resolved_paths,
            skill_settings,
            coordinator,
            resources=(external,),
            user_preferences=user_preferences,
            model_config=model_config,
            project_id=project_id,
            project_cwd=project_cwd,
            job_registry=job_registry,
            job_parent_id=job_parent_id,
            sandbox_launcher=sandbox_launcher,
            sandbox_config=sandbox_config,
        )
        if sandbox_session_id is not None:
            coordinator.bind_session(sandbox_session_id, child_factory, workspace, project_cwd)
        return runner
    except Exception:
        external.close()
        raise


def _build_runner(
    workspace: Path,
    planner_name: PlannerName,
    settings: RunnerSettings,
    tools: ToolExecutor,
    checkpoints: object | None,
    paths: ClientPaths,
    skill_settings: SkillSettings,
    subagents: object | None = None,
    *,
    resources: tuple[object, ...] = (),
    user_preferences: str = "",
    model_config: ModelConfig | None = None,
    project_id: str | None = None,
    project_cwd: Path | None = None,
    job_registry: JobRegistry | None = None,
    job_parent_id: str | None = None,
    sandbox_launcher: SandboxLauncher | None = None,
    sandbox_config: dict[str, object] | None = None,
) -> AgentRunner:
    skills = _user_skill_catalog(paths, skill_settings)
    project_skill_gate = (
        ProjectSkillGate(
            project_cwd or workspace,
            project_id,
            ProjectSkillTrustStore(LocalConfigStore(paths.config_file)),
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
    runner_scope = None
    if job_registry is not None:
        runner_scope = job_registry.root_scope().child(JobScopeKind.THREAD)
    return AgentRunner(
        planner=planner,
        tools=tools,
        max_transport_retries=settings.max_transport_retries,
        max_tool_calls=settings.max_tool_calls,
        log_full_messages=settings.log_full_messages,
        checkpoints=checkpoints,
        skill_catalog=skills,
        skills_enabled=skill_settings.enabled,
        project_skill_gate=project_skill_gate,
        workspace_root=str(workspace.resolve()),
        project_cwd=str(project_cwd.resolve()) if project_cwd is not None else None,
        subagents=subagents,
        resources=resources,
        job_registry=job_registry,
        job_scope=runner_scope,
        parent_job_id=job_parent_id,
        sandbox_launcher=sandbox_launcher,
        sandbox_config=sandbox_config,
    )


def _external_resources(
    paths: ClientPaths,
    config: dict[str, object],
    *,
    job_registry: JobRegistry | None = None,
    job_scope: JobScope | None = None,
) -> ExternalMcpResources:
    if not McpCapabilitySettings.from_config(config).enabled:
        return ExternalMcpResources()
    plan = prepare_mcp_plan(paths)
    servers = plan.effective_servers()
    kwargs = {}
    if job_registry is not None:
        kwargs["job_registry"] = job_registry
    if job_scope is not None:
        kwargs["job_scope"] = job_scope
    return start_external_tools(servers, McpSettings.from_config(config), **kwargs)


def _user_skill_catalog(paths: ClientPaths, settings: SkillSettings) -> SkillCatalog:
    if not settings.enabled:
        return SkillCatalog()
    discovered = SkillCatalog.discover(global_root=paths.skills_dir)
    return SkillCatalog(
        tuple(item for item in discovered.definitions() if item.manifest.parent.name not in settings.disabled)
    )


def _user_skill_read_roots(paths: ClientPaths, settings: SkillSettings) -> tuple[Path, ...]:
    return tuple(item.manifest.parent for item in _user_skill_catalog(paths, settings).definitions())


def _sandbox_runtime(
    config: dict[str, object],
    *,
    paths: ClientPaths | None = None,
) -> tuple[SandboxLauncher | None, dict[str, object]]:
    """Build the run_command launcher when the Broker control plane is healthy."""

    raw = config.get("sandbox_config")
    if not isinstance(raw, dict):
        raw = config.get("sandbox") if isinstance(config.get("sandbox"), dict) else None
    normalized = normalize_sandbox_config(raw) if isinstance(raw, dict) else normalize_sandbox_config()
    proxy_port = int(normalized["proxy_port"])
    broker = WindowsBrokerClient.from_system(expected_proxy_port=proxy_port)
    status = broker.status()
    if not status.installed or not status.healthy:
        return None, normalized
    lease_store_path = paths.runtime_dir / "sandbox-leases.json" if paths is not None else None
    return SandboxLauncher(
        broker=broker,
        admission=SandboxAdmission(),
        lease_store_path=lease_store_path,
    ), normalized


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


def _terminal_type_for_config(config: dict[str, object]) -> str:
    runtime = config.get("runtime")
    values = runtime if isinstance(runtime, dict) else {}
    return effective_terminal_type(values.get("terminal_type", DEFAULT_TERMINAL_TYPE))
