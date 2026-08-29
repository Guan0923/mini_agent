"""Sandbox authorization as the first global before-tool hook operation."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from backend.runtime.core.contracts import InterruptDecision, InterruptRequest
from backend.runtime.core.events import RuntimeEvent
from backend.runtime.core.hooks import HookOperationResult, ToolHookContext

from .decision import SandboxExecutionDecision
from .launcher import SandboxLauncher
from .policy import SandboxLimits


def sandbox_operation(context: ToolHookContext) -> HookOperationResult:
    """Authorize a tool call and return its immutable Sandbox launch decision."""

    if context.outcome is not None:
        return HookOperationResult.continue_execution()

    requires_approval = context.requires_confirmation and context.permission_mode != "full_access"
    if requires_approval:
        request = _approval_request(context)
        _record(context, "approval_requested", "Tool approval requested", request)
        _publish(context, "approval_requested", request.message, request.data)
        if context.interrupt is None:
            decision = InterruptDecision("cancel")
            return HookOperationResult.reject(
                "Tool approval cancelled because no interrupt handler is available.",
                {"interrupt": decision},
            )
        decision = context.interrupt(request)
        if decision.choice == "deny":
            return HookOperationResult.reject(
                f"The user denied this {context.name} tool call.",
                {"interrupt": decision, "failure_code": "user_denied"},
            )
        if decision.choice != "continue":
            return HookOperationResult.reject(
                "Tool approval was not granted.",
                {"interrupt": decision},
            )
        _record(context, "approval_granted", "Tool approval granted", request)
        _publish(context, "approval_granted", request.message, request.data)

    execution_data: dict[str, object] = {}
    if context.name == "run_command":
        if (
            not isinstance(context.sandbox_launcher, SandboxLauncher)
            or context.sandbox_config.get("enabled") is not True
        ):
            return HookOperationResult.reject(
                "Command execution is unavailable because the Sandbox runtime is not healthy.",
                {"failure_code": "sandbox_unavailable"},
            )
        execution_data["sandbox_decision"] = _command_decision(context)
    return HookOperationResult.continue_execution(execution_data)


def _approval_request(context: ToolHookContext) -> InterruptRequest:
    command = context.arguments.get("command")
    permission_target = "full_access" if context.name == "run_command" else context.permission_mode
    return InterruptRequest(
        "tool",
        f"Call tool {context.name}?",
        {
            "run_id": context.run.run_id,
            "tool": context.name,
            "call_id": context.call_id,
            "arguments": dict(context.arguments),
            "session_id": context.run.session_id,
            "command": command if isinstance(command, str) else context.name,
            "cwd": context.workspace_root,
            "permission_target": permission_target,
        },
    )


def _command_decision(context: ToolHookContext) -> SandboxExecutionDecision:
    assert isinstance(context.sandbox_launcher, SandboxLauncher)
    raw_limits = context.sandbox_config.get("limits")
    limits = SandboxLimits.from_mapping(raw_limits if isinstance(raw_limits, Mapping) else None)
    return SandboxExecutionDecision(
        launcher=context.sandbox_launcher,
        workspace=Path(context.workspace_root).resolve(),
        session_id=context.run.session_id,
        user_id=context.sandbox_user_id or "local",
        limits=limits,
    )


def _record(context: ToolHookContext, kind: str, message: str, request: InterruptRequest) -> None:
    if context.record_event is not None:
        context.record_event(kind, message, {"interrupt_kind": "tool", **request.data})


def _publish(context: ToolHookContext, kind: str, message: str, data: dict[str, object]) -> None:
    if context.publish is not None:
        context.publish(RuntimeEvent(kind, message, data))


__all__ = ["sandbox_operation"]
