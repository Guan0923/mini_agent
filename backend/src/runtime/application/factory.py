"""The composition root for a local-first Mini-Agent client."""

from __future__ import annotations

from pathlib import Path
from typing import Literal
from uuid import uuid4

from backend.configuration import ClientPaths, UserConfigStore, initialize_config, load_config, section
from backend.domain import DEFAULT_TIME_ZONE
from backend.domain.memory import MemorySettings
from backend.domain.terminal import DEFAULT_TERMINAL_TYPE
from backend.jobs import JobRegistry, JobScope, JobScopeKind
from backend.mcp.client import ExternalMcpResources, start_external_tools
from backend.mcp.config import McpSettings, prepare_mcp_plan
from backend.planning import LLMPlanner, RuleBasedPlanner
from backend.providers import LLMClient, ModelConfig
from backend.sandbox import (
    NetworkMode,
    NetworkRule,
    PermissionMode,
    SandboxAdmission,
    SandboxInitializationError,
    SandboxLauncher,
    SandboxLimits,
    SandboxPolicy,
    WindowsBrokerClient,
)
from backend.skills import ProjectSkillGate, ProjectSkillTrustStore, SkillCatalog
from backend.storage.memory import MemoryStore
from backend.storage.settings_contract import normalize_sandbox_config
from backend.storage.sqlite import SQLiteSessionStore
from backend.sync import RequestsSyncTransport, SyncClient, SyncCoordinator
from backend.tools import ToolExecutor, WorkspaceFiles, build_tool_registry, delegation_tools
from backend.tools.terminal import effective_terminal_type

from ..capability_settings import SkillSettings, SubagentSettings
from ..conversation.references import FileReferenceExpander
from ..core.config import RunnerSettings, log_full_messages_from_toml
from ..execution.runner import AgentRunner
from ..instructions import discover_agent_instructions
from ..memory import MemoryContextSelector, MemoryDiagnosticsRegistry, MemoryPromptInjector
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
    *,
    paths: ClientPaths | None = None,
    user_preferences: str = "",
    model_config: ModelConfig | None = None,
    config_override: dict[str, object] | None = None,
    default_timezone: str = DEFAULT_TIME_ZONE,
    session_provisioner: object | None = None,
    session_provisioner_cleanup: object | None = None,
    project_id: str | None = None,
    upload_root: Path | None = None,
    agents_home: Path | None = None,
    job_registry: JobRegistry | None = None,
    job_user_id: str | None = None,
    job_parent_id: str | None = None,
    sandbox_session_id: str | None = None,
    memory_diagnostics: MemoryDiagnosticsRegistry | None = None,
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
    upload_files = WorkspaceFiles(upload_root) if upload_root is not None else None
    runner_args = (
        workspace,
        planner_name,
        resolved,
        config,
        store,
        files,
        resolved_paths,
        user_preferences,
        upload_files,
    )
    if model_config is None:
        runner = _build_subagent_runner(
            *runner_args,
            **({"agents_home": agents_home} if agents_home is not None else {}),
            **({"project_id": project_id} if project_id else {}),
            **({"job_registry": job_registry} if job_registry is not None else {}),
            **({"job_user_id": job_user_id} if job_user_id is not None else {}),
            **({"job_parent_id": job_parent_id} if job_parent_id is not None else {}),
            **({"sandbox_session_id": sandbox_session_id} if sandbox_session_id is not None else {}),
            **({"memory_diagnostics": memory_diagnostics} if memory_diagnostics is not None else {}),
        )
    else:
        runner = _build_subagent_runner(
            *runner_args,
            model_config=model_config,
            **({"agents_home": agents_home} if agents_home is not None else {}),
            **({"project_id": project_id} if project_id else {}),
            **({"job_registry": job_registry} if job_registry is not None else {}),
            **({"job_user_id": job_user_id} if job_user_id is not None else {}),
            **({"job_parent_id": job_parent_id} if job_parent_id is not None else {}),
            **({"sandbox_session_id": sandbox_session_id} if sandbox_session_id is not None else {}),
            **({"memory_diagnostics": memory_diagnostics} if memory_diagnostics is not None else {}),
        )
    try:
        sync_coordinator = _build_sync_coordinator(
            config, store, **({"job_registry": job_registry} if job_registry is not None else {})
        )
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
    upload_files: WorkspaceFiles | None = None,
    *,
    agents_home: Path | None = None,
    model_config: ModelConfig | None = None,
    project_id: str | None = None,
    job_registry: JobRegistry | None = None,
    job_user_id: str | None = None,
    job_parent_id: str | None = None,
    terminal_type: str | None = None,
    sandbox_session_id: str | None = None,
    memory_diagnostics: MemoryDiagnosticsRegistry | None = None,
) -> AgentRunner:
    terminal_type = terminal_type or _terminal_type_for_config(config)
    resolved_paths = paths or client_paths()
    skill_settings = SkillSettings.from_config(config)
    subagent_settings = SubagentSettings.from_config(config)
    raw_memory = config.get("memory")
    memory_settings = MemorySettings.from_mapping(raw_memory if isinstance(raw_memory, dict) else None)
    sandbox_launcher, sandbox_config = _sandbox_runtime(config)

    def child_factory() -> AgentRunner:
        return _build_runner(
            workspace,
            planner_name,
            settings,
            build_tool_registry(
                workspace,
                upload_files=upload_files,
                terminal_type=terminal_type,
                sandbox_config=sandbox_config,
            ),
            checkpoints,
            resolved_paths,
            skill_settings,
            user_preferences=user_preferences,
            agents_home=agents_home,
            model_config=model_config,
            project_id=project_id,
            job_registry=job_registry,
            job_user_id=job_user_id,
            job_parent_id=job_parent_id,
            sandbox_launcher=sandbox_launcher,
            sandbox_config=sandbox_config,
            memory_settings=memory_settings,
            memory_diagnostics=memory_diagnostics,
        )

    coordinator = SubagentCoordinator(child_factory, workspace, subagent_settings)
    mcp_scope: JobScope | None = None
    if job_registry is not None:
        mcp_scope = job_registry.root_scope().child(JobScopeKind.USER, user_id=job_user_id)
    external = _external_resources(
        workspace,
        resolved_paths,
        config,
        job_registry=job_registry,
        job_scope=mcp_scope,
        session_id=sandbox_session_id,
        sandbox_launcher=sandbox_launcher,
        sandbox_config=sandbox_config,
        sandbox_user_id=job_user_id,
    )
    try:
        tools = build_tool_registry(
            workspace,
            workspace_files=files,
            upload_files=upload_files,
            terminal_type=terminal_type,
            sandbox_config=sandbox_config,
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
            checkpoints,
            resolved_paths,
            skill_settings,
            coordinator,
            resources=(external,),
            user_preferences=user_preferences,
            agents_home=agents_home,
            model_config=model_config,
            project_id=project_id,
            job_registry=job_registry,
            job_user_id=job_user_id,
            job_parent_id=job_parent_id,
            sandbox_launcher=sandbox_launcher,
            sandbox_config=sandbox_config,
            memory_settings=memory_settings,
            memory_diagnostics=memory_diagnostics,
        )
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
    agents_home: Path | None = None,
    model_config: ModelConfig | None = None,
    project_id: str | None = None,
    job_registry: JobRegistry | None = None,
    job_user_id: str | None = None,
    job_parent_id: str | None = None,
    sandbox_launcher: SandboxLauncher | None = None,
    sandbox_config: dict[str, object] | None = None,
    memory_settings: MemorySettings | None = None,
    memory_diagnostics: MemoryDiagnosticsRegistry | None = None,
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
        agent_instructions = discover_agent_instructions(
            global_root=agents_home,
            project_root=workspace,
        ).render()
        memory_settings = memory_settings or MemorySettings()
        memory_store = MemoryStore(paths)
        memory_injector = MemoryPromptInjector(
            MemoryContextSelector(memory_store, memory_settings),
            memory_settings,
            user_id=job_user_id or "",
            project_id=project_id,
            diagnostics=memory_diagnostics,
        )
        planner = LLMPlanner(
            LLMClient(model_config or ModelConfig.from_toml(paths.config_file)),
            tools.specs(),
            tools.read_only_specs(),
            user_preferences=user_preferences,
            agent_instructions=agent_instructions,
            memory_prompt_injector=memory_injector,
        )
    runner_scope = None
    if job_registry is not None:
        owner_scope = job_registry.root_scope()
        if job_user_id is not None:
            owner_scope = owner_scope.child(JobScopeKind.USER, user_id=job_user_id)
        runner_scope = owner_scope.child(JobScopeKind.RUNNER)
    return AgentRunner(
        planner=planner,
        tools=tools,
        max_transport_retries=settings.max_transport_retries,
        max_tool_calls=settings.max_tool_calls,
        log_full_messages=settings.log_full_messages,
        checkpoints=checkpoints,
        skill_catalog=skills,
        skill_auto_select=skill_settings.auto_select,
        project_skill_gate=project_skill_gate,
        workspace_root=str(workspace.resolve()),
        subagents=subagents,
        resources=resources,
        job_registry=job_registry,
        job_scope=runner_scope,
        parent_job_id=job_parent_id,
        sandbox_launcher=sandbox_launcher,
        sandbox_config=sandbox_config,
        sandbox_user_id=job_user_id,
    )


def _external_resources(
    workspace: Path,
    paths: ClientPaths,
    config: dict[str, object],
    *,
    job_registry: JobRegistry | None = None,
    job_scope: JobScope | None = None,
    session_id: str | None = None,
    sandbox_launcher: SandboxLauncher | None = None,
    sandbox_config: dict[str, object] | None = None,
    sandbox_user_id: str | None = None,
) -> ExternalMcpResources:
    plan = prepare_mcp_plan(paths)
    servers = plan.effective_servers()
    if servers and (sandbox_launcher is None or sandbox_config is None):
        raise SandboxInitializationError("External MCP tools require a healthy Sandbox runtime.")
    kwargs = {}
    if job_registry is not None:
        kwargs["job_registry"] = job_registry
    if job_scope is not None:
        kwargs["job_scope"] = job_scope
    if sandbox_launcher is not None and sandbox_config is not None:
        kwargs["sandbox_launcher"] = sandbox_launcher
        kwargs["sandbox_policy_factory"] = _sandbox_policy_factory(
            workspace,
            session_id=session_id or "mcp",
            config=sandbox_config,
        )
        kwargs["sandbox_user_id"] = sandbox_user_id
    return start_external_tools(servers, McpSettings.from_config(config), **kwargs)


def _sandbox_runtime(config: dict[str, object]) -> tuple[SandboxLauncher, dict[str, object]]:
    """Build the mandatory process launcher or fail before a Runner can be used."""

    raw = config.get("sandbox_config")
    if not isinstance(raw, dict):
        raw = config.get("sandbox") if isinstance(config.get("sandbox"), dict) else None
    normalized = normalize_sandbox_config(raw) if isinstance(raw, dict) else normalize_sandbox_config()
    broker = WindowsBrokerClient.from_system()
    status = broker.status()
    if not status.installed:
        raise SandboxInitializationError("Windows Sandbox Broker 未安装或当前不可用。")
    if not status.healthy:
        raise SandboxInitializationError("Windows Sandbox Broker 已安装，但健康检查未通过。")
    return SandboxLauncher(broker=broker, admission=SandboxAdmission()), normalized


def _sandbox_policy_factory(workspace: Path, *, session_id: str, config: dict[str, object]):
    def make_policy(server) -> SandboxPolicy:
        raw_rules = config.get("network_allowlist")
        rules = (
            tuple(
                NetworkRule(str(item.get("host") or ""), int(item.get("port")))
                for item in raw_rules
                if isinstance(item, dict)
            )
            if isinstance(raw_rules, (list, tuple))
            else ()
        )
        file_mode = PermissionMode(str(config.get("file_mode", PermissionMode.READ_ONLY.value)))
        network_mode = NetworkMode(str(config.get("network_mode", NetworkMode.NO_NETWORK.value)))
        if file_mode is PermissionMode.FULL_ACCESS:
            network_mode = NetworkMode.FULL_NETWORK
        limits = SandboxLimits.from_mapping(config.get("limits") if isinstance(config.get("limits"), dict) else None)
        return SandboxPolicy(
            workspace=workspace,
            session_id=session_id,
            job_id=f"mcp-{server.name}-{uuid4().hex}",
            file_mode=file_mode,
            network_mode=network_mode,
            network_allowlist=rules,
            limits=limits,
            enforced=file_mode is not PermissionMode.FULL_ACCESS,
            full_access_acknowledged=file_mode is PermissionMode.FULL_ACCESS,
        )

    return make_policy


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


def _build_sync_coordinator(
    config: dict[str, object], store: SQLiteSessionStore, *, job_registry: JobRegistry | None = None
) -> SyncCoordinator | None:
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
        job_registry=job_registry,
    )
    store.set_sync_listener(coordinator.notify)
    coordinator.start()
    return coordinator
