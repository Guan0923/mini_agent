"""Shared composition helpers for authenticated runtime operations."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from backend.domain import DEFAULT_TIME_ZONE
from backend.providers import ModelConfig
from backend.runtime import RunnerSettings, build_application

from ..state import WebAppState

if TYPE_CHECKING:
    from backend.runtime.application.services import AgentApplication


def build_user_application(
    state: WebAppState,
    user_id: str,
    *,
    session_id: str,
    user_preferences: str = "",
    model_config: ModelConfig | None = None,
    load_model_config: bool = True,
    builder: Callable[..., AgentApplication] | None = None,
) -> AgentApplication:
    """Build an application with the canonical per-user runtime settings."""

    application_builder = builder or build_application
    runtime_config = state.runtime_config_for_user(user_id)
    runtime_values = runtime_config.get("runtime", {})
    log_full_messages = runtime_values.get("log_full_messages", True) if isinstance(runtime_values, dict) else True
    if not isinstance(log_full_messages, bool):
        log_full_messages = True
    max_tool_calls = runtime_values.get("max_tool_calls", 32) if isinstance(runtime_values, dict) else 32
    if not isinstance(max_tool_calls, int) or isinstance(max_tool_calls, bool):
        max_tool_calls = 32
    application = application_builder(
        state.user_workspace(user_id, session_id),
        planner_name="llm",
        settings=RunnerSettings(
            max_transport_retries=5,
            max_tool_calls=max_tool_calls,
            log_full_messages=log_full_messages,
        ),
        project_mcp_enabled=False,
        user_preferences=user_preferences,
        paths=state.user_paths(user_id),
        model_config=state.model_config_for_user(user_id)
        if load_model_config and model_config is None
        else model_config,
        config_override=runtime_config,
        default_timezone=str(state.agent_config_for_user(user_id).get("timezone", DEFAULT_TIME_ZONE)),
    )
    if state.snapshot_manager is not None:
        store = getattr(application, "session_store", None) or getattr(application, "store", None)
        if callable(getattr(store, "set_sync_listener", None)):
            store.set_sync_listener(lambda: state.mark_sync_dirty(user_id))
    return application
