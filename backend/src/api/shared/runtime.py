"""Shared composition helpers for local runtime operations."""

from __future__ import annotations

import inspect
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from backend.domain import DEFAULT_TIME_ZONE
from backend.jobs import JobRegistry
from backend.providers import ModelConfig
from backend.runtime import RunnerSettings, build_application

from ..state import WebAppState

if TYPE_CHECKING:
    from backend.runtime.application.services import AgentApplication


def build_local_application(
    state: WebAppState,
    *,
    session_id: str,
    user_preferences: str = "",
    model_config: ModelConfig | None = None,
    load_model_config: bool = True,
    builder: Callable[..., AgentApplication] | None = None,
    workspace: Path | None = None,
    session_provisioner: Callable[..., object] | None = None,
    session_provisioner_cleanup: Callable[[str], None] | None = None,
    project_id: str | None = None,
    project_cwd: Path | None = None,
    job_registry: JobRegistry | None = None,
    job_parent_id: str | None = None,
) -> AgentApplication:
    """Build an application with the canonical local runtime settings."""
    application_builder = builder or build_application
    runtime_config = state.runtime_config()
    runtime_values = runtime_config.get("runtime", {})
    log_full_messages = runtime_values.get("log_full_messages", True) if isinstance(runtime_values, dict) else True
    if not isinstance(log_full_messages, bool):
        log_full_messages = True
    max_tool_calls = runtime_values.get("max_tool_calls", 32) if isinstance(runtime_values, dict) else 32
    if not isinstance(max_tool_calls, int) or isinstance(max_tool_calls, bool):
        max_tool_calls = 32
    if session_provisioner is None:
        session_provisioner = _project_session_provisioner(state)
    if session_provisioner_cleanup is None:
        session_provisioner_cleanup = _project_session_provisioner_cleanup(state)
    builder_options = {
        "planner_name": "llm",
        "settings": RunnerSettings(
            max_transport_retries=5,
            max_tool_calls=max_tool_calls,
            log_full_messages=log_full_messages,
        ),
        "user_preferences": user_preferences,
        "paths": state.paths,
        "model_config": state.model_config() if load_model_config and model_config is None else model_config,
        "config_override": {
            **runtime_config,
            "sandbox_config": state.settings.sandbox_config(),
        },
        "default_timezone": str(state.agent_config().get("timezone", DEFAULT_TIME_ZONE)),
        "session_provisioner": session_provisioner,
        "session_provisioner_cleanup": session_provisioner_cleanup,
        "project_id": project_id or None,
        "project_cwd": project_cwd,
        "job_registry": job_registry or getattr(state, "job_registry", None),
        "job_parent_id": job_parent_id,
        "sandbox_session_id": session_id,
        "agent_thread_index": getattr(state, "agent_thread_index", None),
        "subagent_coordinator": getattr(state, "subagent_coordinator", None),
        "sandbox_maintenance_gate": getattr(state, "sandbox_maintenance", None),
        "todo_store": getattr(state, "todo_store", None),
    }
    # Preserve compatibility with embedders/tests that accept only a subset
    # of the canonical application builder options.
    try:
        signature = inspect.signature(application_builder)
    except (TypeError, ValueError):
        signature = None
    if signature is not None and not any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in signature.parameters.values()
    ):
        builder_options = {
            name: value
            for name, value in builder_options.items()
            if name in signature.parameters and signature.parameters[name].kind is not inspect.Parameter.POSITIONAL_ONLY
        }
    state.paths.ensure_session(session_id)
    application = application_builder(workspace or state.paths.session_workspace(session_id), **builder_options)
    # Provider credentials are resolved lazily for each model boundary.  The
    # selected provider name comes from the dynamic node; this callback keeps
    # secrets in the per-user settings database and out of RuntimeState.
    runner = getattr(application, "runner", None)
    if runner is not None:

        def resolver(provider_name: str):
            return state.model_config(provider_name)

        setattr(runner, "provider_config_resolver", resolver)
        planner = getattr(runner, "planner", None)
        client = getattr(planner, "client", None)
        setter = getattr(client, "set_config_resolver", None)
        if callable(setter):
            setter(resolver)
    return application


def _project_session_provisioner(state: WebAppState):
    def provision(store, title: str, source_session):
        project = state.projects.session_project(source_session.session_id)
        if project is None:
            return None
        if project.removed_at is not None:
            raise RuntimeError("项目已移除，请从回收站恢复后再运行。")
        if not project.available:
            raise RuntimeError("项目 cwd 不可访问，请恢复文件夹后重试。")
        session = store.create_session(title)
        try:
            state.projects.create_session(project.project_id, session.session_id)
            state.copy_session_uploads(source_session.session_id, session.session_id)
        except Exception:
            try:
                state.projects.discard_session(session.session_id)
            finally:
                paths = getattr(store, "paths", None)
                if paths is not None:
                    shutil.rmtree(paths.session_root(session.session_id), ignore_errors=True)
            raise
        return session

    return provision


def _project_session_provisioner_cleanup(state: WebAppState):
    def cleanup(session_id: str) -> None:
        state.projects.discard_session(session_id)

    return cleanup
