"""Application composition and the agent execution loop."""

from __future__ import annotations

from importlib import import_module
from typing import Any

_EXPORTS = {
    "AgentApplication": ("application", "AgentApplication"),
    "AgentRunner": ("runner", "AgentRunner"),
    "AgentRuntime": ("context", "AgentRuntime"),
    "ArtifactStore": ("artifacts", "ArtifactStore"),
    "CheckpointStore": ("checkpointing", "CheckpointStore"),
    "ConversationService": ("conversations", "ConversationService"),
    "InMemoryArtifactStore": ("artifacts", "InMemoryArtifactStore"),
    "LegacyAgentRunner": ("runner", "LegacyAgentRunner"),
    "PreparedResponse": ("context", "PreparedResponse"),
    "RunnerSettings": ("config", "RunnerSettings"),
    "log_full_messages_from_env": ("config", "log_full_messages_from_env"),
    "RunSummary": ("context", "RunSummary"),
    "RuntimeExchange": ("context", "RuntimeExchange"),
    "RuntimeEvent": ("events", "RuntimeEvent"),
    "RuntimeRunner": ("execution", "RuntimeRunner"),
    "RuntimeServices": ("context", "RuntimeServices"),
    "RuntimeState": ("context", "RuntimeState"),
    "SessionStore": ("session_store", "SessionStore"),
    "SteeringHandler": ("contracts", "SteeringHandler"),
    "SQLiteCheckpointStore": ("checkpoints", "SQLiteCheckpointStore"),
    "SQLiteSessionStore": ("sessions", "SQLiteSessionStore"),
    "TaskPreparationError": ("conversations", "TaskPreparationError"),
    "TaskPreprocessor": ("conversations", "TaskPreprocessor"),
    "build_application": ("factory", "build_application"),
    "build_runner": ("factory", "build_runner"),
    "build_session_store": ("factory", "build_session_store"),
}

__all__ = list(_EXPORTS)


def __getattr__(name: str) -> Any:
    """Resolve public exports lazily to keep submodule imports isolated."""

    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attribute = target
    value = getattr(import_module(f".{module_name}", __name__), attribute)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted({*globals(), *__all__})
