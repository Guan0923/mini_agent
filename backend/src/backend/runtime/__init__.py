"""Application composition and the agent execution loop."""

from __future__ import annotations

from importlib import import_module
from typing import Any

_EXPORTS = {
    "AgentApplication": ("application.services", "AgentApplication"),
    "AgentHook": ("core.hooks", "AgentHook"),
    "AgentRunner": ("execution.runner", "AgentRunner"),
    "AgentRuntime": ("core.context", "AgentRuntime"),
    "CheckpointStore": ("persistence.checkpointing", "CheckpointStore"),
    "CancellationHandler": ("core.contracts", "CancellationHandler"),
    "ConversationService": ("conversation.service", "ConversationService"),
    "HookCancellation": ("core.hooks", "HookCancellation"),
    "HookErrorInfo": ("core.hooks", "HookErrorInfo"),
    "HookExecutionError": ("core.hooks", "HookExecutionError"),
    "HookManager": ("core.hooks", "HookManager"),
    "HookOutcome": ("core.hooks", "HookOutcome"),
    "LegacyAgentRunner": ("execution.legacy", "LegacyAgentRunner"),
    "PreparedResponse": ("core.context", "PreparedResponse"),
    "QuestionOption": ("core.contracts", "QuestionOption"),
    "ModelHookContext": ("core.hooks", "ModelHookContext"),
    "ModelHookResult": ("core.hooks", "ModelHookResult"),
    "RunnerSettings": ("core.config", "RunnerSettings"),
    "log_full_messages_from_toml": ("core.config", "log_full_messages_from_toml"),
    "RunSummary": ("core.context", "RunSummary"),
    "RunHookContext": ("core.hooks", "RunHookContext"),
    "RunHookInfo": ("core.hooks", "RunHookInfo"),
    "RunHookResult": ("core.hooks", "RunHookResult"),
    "RuntimeExchange": ("core.context", "RuntimeExchange"),
    "RuntimeEvent": ("core.events", "RuntimeEvent"),
    "RuntimeRunner": ("execution", "RuntimeRunner"),
    "RuntimeServices": ("core.context", "RuntimeServices"),
    "RuntimeState": ("core.context", "RuntimeState"),
    "SessionStore": ("conversation.ports", "SessionStore"),
    "SteeringHandler": ("core.contracts", "SteeringHandler"),
    "SuspensionHandler": ("core.contracts", "SuspensionHandler"),
    "TaskPreparationError": ("conversation.service", "TaskPreparationError"),
    "TaskPreprocessor": ("conversation.ports", "TaskPreprocessor"),
    "ToolHookContext": ("core.hooks", "ToolHookContext"),
    "ToolHookResult": ("core.hooks", "ToolHookResult"),
    "UserQuestion": ("core.contracts", "UserQuestion"),
    "REQUEST_USER_INPUT_SPEC": ("conversation.user_input", "REQUEST_USER_INPUT_SPEC"),
    "REQUEST_PLAN_REVIEW_SPEC": ("planning.review", "REQUEST_PLAN_REVIEW_SPEC"),
    "build_application": ("application.factory", "build_application"),
    "build_runner": ("application.factory", "build_runner"),
    "build_session_store": ("application.factory", "build_session_store"),
    "AgentExecutor": ("executor", "AgentExecutor"),
    "ExecutorDependencies": ("executor", "ExecutorDependencies"),
    "RuntimeStateNode": ("state_tree", "RuntimeState"),
    "CanonicalRuntimeState": ("state_tree", "RuntimeState"),
    "RuntimeStateTree": ("state_tree", "RuntimeStateTree"),
    "NodeFrame": ("state_tree", "NodeFrame"),
    "NodeWriter": ("state_tree", "NodeWriter"),
    "RuntimeNodeStore": ("state_tree", "RuntimeNodeStore"),
    "change_payload": ("state_tree", "change_payload"),
    "compaction_payload": ("state_tree", "compaction_payload"),
    "message_payload": ("state_tree", "message_payload"),
    "recoverable": ("state_tree", "recoverable"),
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
