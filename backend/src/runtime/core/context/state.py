"""Serializable runtime state and completed-run summaries."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from backend.domain import (
    DEFAULT_TIME_ZONE,
    AssistantMessage,
    ChatMessage,
    RunState,
    ToolSpec,
    message_to_dict,
    normalize_provider_options,
)
from backend.domain.messages import messages_from_dicts
from backend.domain.state import utc_now

from ..config import RunnerSettings

RuntimeOperation = Literal[
    "skill_selection",
    "decision",
    "plan",
    "summarize",
    "title",
    "finalize",
]
OutputMode = Literal["text", "json", "tools"]
RuntimeStatus = Literal["idle", "running"]


@dataclass(frozen=True)
class RunSummary:
    run_id: str
    task: str
    status: str
    mode: str
    final_answer: str | None = None
    workflow_id: str | None = None
    attempt: int = 1


@dataclass
class RuntimeState:
    """Serializable state required to resume one complete conversation session."""

    session_id: str
    workspace_root: str | None = None
    project_cwd: str | None = None
    timezone: str = DEFAULT_TIME_ZONE
    messages: list[ChatMessage] = field(default_factory=list)
    provider: str = "unknown"
    model: str = "unknown"
    provider_name: str = "unknown"
    model_snapshot: dict[str, Any] = field(default_factory=dict)
    permission_mode: str = "read_only"
    running_mode: str = "agent"
    request_parameters: dict[str, Any] = field(default_factory=dict)
    runner_settings: RunnerSettings = field(default_factory=RunnerSettings)
    tool_specs: list[ToolSpec] = field(default_factory=list)
    current_run: RunState | None = None
    run_history: list[RunSummary] = field(default_factory=list)
    active_message: AssistantMessage | None = None
    active_tool_index: int | None = None
    usage: dict[str, Any] | None = None
    turn_usage: dict[str, Any] | None = None
    token_usage: dict[str, Any] = field(default_factory=dict)
    status: RuntimeStatus = "idle"
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "workspace_root": self.workspace_root,
            "project_cwd": self.project_cwd,
            "timezone": self.timezone,
            "messages": [message_to_dict(message) for message in self.messages],
            "provider": self.provider,
            "model": self.model,
            "provider_name": self.provider_name,
            "model_snapshot": self.model_snapshot,
            "permission_mode": self.permission_mode,
            "running_mode": self.running_mode,
            "request_parameters": self.request_parameters,
            "runner_settings": {
                "max_transport_retries": self.runner_settings.max_transport_retries,
                "max_tool_calls": self.runner_settings.max_tool_calls,
                "log_full_messages": self.runner_settings.log_full_messages,
            },
            "tool_specs": [
                {
                    "name": spec.name,
                    "description": spec.description,
                    "parameters": spec.parameters,
                    "provider_options": spec.provider_options,
                }
                for spec in self.tool_specs
            ],
            "current_run": (self.current_run.to_dict() if self.current_run else None),
            "run_history": [summary.__dict__ for summary in self.run_history],
            "active_message": message_to_dict(self.active_message) if self.active_message else None,
            "active_tool_index": self.active_tool_index,
            "usage": self.usage,
            "turn_usage": self.turn_usage,
            "token_usage": self.token_usage,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RuntimeState:
        raw_settings = data.get("runner_settings") or {}
        legacy_tool_calls = raw_settings.get("max_actions") if isinstance(raw_settings, dict) else None
        settings = {
            key: raw_settings[key]
            for key in ("max_transport_retries", "max_tool_calls", "log_full_messages")
            if isinstance(raw_settings, dict) and key in raw_settings
        }
        if (
            "max_tool_calls" not in settings
            and isinstance(legacy_tool_calls, int)
            and not isinstance(legacy_tool_calls, bool)
        ):
            settings["max_tool_calls"] = legacy_tool_calls
        active_data = data.get("active_message")
        active_message = None
        if isinstance(active_data, dict):
            parsed = messages_from_dicts([active_data])[0]
            if not isinstance(parsed, AssistantMessage):
                raise ValueError("Runtime active_message must be an AssistantMessage.")
            active_message = parsed
        return cls(
            session_id=str(data["session_id"]),
            workspace_root=(str(data["workspace_root"]) if data.get("workspace_root") is not None else None),
            project_cwd=(str(data["project_cwd"]) if data.get("project_cwd") is not None else None),
            timezone=str(data.get("timezone") or DEFAULT_TIME_ZONE),
            messages=messages_from_dicts([dict(item) for item in data.get("messages", [])]),
            provider=(
                "chat_completions"
                if str(data.get("provider") or "").casefold() == "deepseek"
                else str(data.get("provider") or "unknown")
            ),
            model=str(data.get("model") or "unknown"),
            provider_name=(
                "default"
                if str(data.get("provider_name") or data.get("provider") or "").casefold() == "deepseek"
                else str(data.get("provider_name") or data.get("provider") or "unknown")
            ),
            model_snapshot=dict(data.get("model_snapshot") or {}),
            permission_mode=str(data.get("permission_mode") or "read_only"),
            running_mode=str(data.get("running_mode") or "agent"),
            request_parameters=dict(data.get("request_parameters") or {}),
            runner_settings=RunnerSettings(**settings),
            tool_specs=[
                ToolSpec(
                    name=str(item["name"]),
                    description=str(item.get("description") or ""),
                    parameters=dict(item.get("parameters") or {}),
                    provider_options=normalize_provider_options(item.get("provider_options")),
                )
                for item in data.get("tool_specs", [])
            ],
            current_run=RunState.from_dict(data["current_run"]) if data.get("current_run") else None,
            run_history=[RunSummary(**item) for item in data.get("run_history", [])],
            active_message=active_message,
            active_tool_index=data.get("active_tool_index"),
            usage=data.get("usage") if isinstance(data.get("usage"), dict) else None,
            turn_usage=data.get("turn_usage") if isinstance(data.get("turn_usage"), dict) else None,
            token_usage=dict(data.get("token_usage") or {}),
            status="running" if data.get("status") == "running" else "idle",
            created_at=str(data.get("created_at") or utc_now()),
            updated_at=str(data.get("updated_at") or utc_now()),
        )
