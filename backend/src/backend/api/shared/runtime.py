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
    user_preferences: str = "",
    model_config: ModelConfig | None = None,
    load_model_config: bool = True,
    builder: Callable[..., AgentApplication] | None = None,
) -> AgentApplication:
    """Build an application with the canonical per-user runtime settings."""

    application_builder = builder or build_application
    return application_builder(
        state.user_workspace(user_id),
        planner_name="llm",
        settings=RunnerSettings(log_full_messages=True),
        project_mcp_enabled=False,
        user_preferences=user_preferences,
        paths=state.user_paths(user_id),
        model_config=state.model_config_for_user(user_id)
        if load_model_config and model_config is None
        else model_config,
        config_override=state.runtime_config_for_user(user_id),
        default_timezone=str(state.agent_config_for_user(user_id).get("timezone", DEFAULT_TIME_ZONE)),
    )
