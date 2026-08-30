"""Sandbox authorization as the first global before-tool hook operation."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from backend.runtime.core.contracts import InterruptDecision, InterruptRequest
from backend.runtime.core.events import RuntimeEvent
from backend.runtime.core.hook_contracts import HookOperationResult, ToolHookContext

from ..policy import FileAccessMode, NetworkMode, NetworkRule, ResourceLimits
from ..runtime.launcher import SandboxLauncher
from .decision import SandboxExecutionDecision

_WEB_NETWORK_TOOLS = frozenset({"web_search", "web_fetch"})


def sandbox_operation(context: ToolHookContext) -> HookOperationResult:
    """Authorize a tool call and return its immutable Sandbox launch decision."""

    if context.outcome is not None:
        return HookOperationResult.continue_execution()

    requires_approval = _requires_approval(context)
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
        if not isinstance(context.sandbox_launcher, SandboxLauncher):
            return HookOperationResult.reject(
                "Command execution is unavailable because the Sandbox runtime is not healthy.",
                {"failure_code": "sandbox_unavailable"},
            )
        execution_data["sandbox_decision"] = _command_decision(context)
    return HookOperationResult.continue_execution(execution_data)


def _requires_approval(context: ToolHookContext) -> bool:
    if context.name in _WEB_NETWORK_TOOLS:
        network_mode = str(context.sandbox_config.get("network_mode") or NetworkMode.NO_NETWORK.value)
        return network_mode != NetworkMode.FULL_NETWORK.value
    workspace_write_allowed = context.permission_mode == "workspace_write" and context.workspace_confined
    return context.requires_confirmation and context.permission_mode != "full_access" and not workspace_write_allowed


def _approval_request(context: ToolHookContext) -> InterruptRequest:
    command = context.arguments.get("command")
    permission_target = context.permission_mode
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
            "network_target": {
                "mode": str(context.sandbox_config.get("network_mode") or "no_network"),
                "allowlist": list(context.sandbox_config.get("network_allowlist") or []),
            }
            if context.name == "run_command"
            else None,
        },
    )


def _command_decision(context: ToolHookContext) -> SandboxExecutionDecision:
    assert isinstance(context.sandbox_launcher, SandboxLauncher)
    raw_limits = context.sandbox_config.get("limits")
    limits = ResourceLimits.from_mapping(raw_limits if isinstance(raw_limits, Mapping) else None)
    try:
        file_mode = FileAccessMode(context.permission_mode)
        network_mode = NetworkMode(str(context.sandbox_config.get("network_mode") or NetworkMode.NO_NETWORK.value))
        proxy_port = int(context.sandbox_config.get("proxy_port", 17831))
        raw_rules = context.sandbox_config.get("network_allowlist")
        if isinstance(raw_rules, (list, tuple)):
            for item in raw_rules:
                if not isinstance(item, Mapping):
                    continue
                port = item.get("port")
                if port is not None and (isinstance(port, bool) or not isinstance(port, int)):
                    raise ValueError("run_command sandbox network port is invalid")
        network_allowlist = (
            tuple(
                NetworkRule(
                    str(item.get("host") or ""),
                    item.get("port"),
                )
                for item in raw_rules
                if isinstance(item, Mapping)
            )
            if isinstance(raw_rules, (list, tuple))
            else ()
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("run_command sandbox configuration is invalid") from exc
    workspaces = [Path(context.workspace_root).resolve()]
    if context.project_cwd:
        project_workspace = Path(context.project_cwd).resolve()
        if project_workspace not in workspaces:
            workspaces.append(project_workspace)
    return SandboxExecutionDecision(
        launcher=context.sandbox_launcher,
        workspaces=tuple(workspaces),
        session_id=context.run.session_id,
        user_id=context.sandbox_user_id or "local",
        file_mode=file_mode,
        network_mode=network_mode,
        network_allowlist=network_allowlist,
        proxy_port=proxy_port,
        limits=limits,
    )


def _record(context: ToolHookContext, kind: str, message: str, request: InterruptRequest) -> None:
    if context.record_event is not None:
        context.record_event(kind, message, {"interrupt_kind": "tool", **request.data})


def _publish(context: ToolHookContext, kind: str, message: str, data: dict[str, object]) -> None:
    if context.publish is not None:
        context.publish(RuntimeEvent(kind, message, data))


__all__ = ["sandbox_operation"]
