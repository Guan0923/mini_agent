from __future__ import annotations

import os
from pathlib import Path

import pytest

from backend.domain import AssistantMessage, ToolMessage, UserMessage
from backend.planning.model_requests import ModelRequestExecutor
from backend.runtime import AgentRunner
from backend.runtime.core.context import PreparedResponse
from backend.runtime.core.contracts import InterruptDecision
from backend.runtime.core.hooks import (
    HookExecutionError,
    HookManager,
    HookOperationResult,
    RunHookContext,
    RunHookInfo,
    SequentialHookManager,
    after_model_hook_manager,
    after_run_hook_manager,
    after_tool_hook_manager,
    before_model_hook_manager,
    before_run_hook_manager,
    before_tool_hook_manager,
)
from backend.sandbox import (
    NetworkMode,
    PermissionMode,
    SandboxExecutionDecision,
    SandboxLauncher,
    SandboxLimits,
)
from backend.sandbox.control.operation import sandbox_operation
from backend.tools import Tool, ToolError, ToolInvocationContext, ToolRegistry, WorkspaceCommand


def test_six_global_managers_are_independent_and_sandbox_is_first() -> None:
    managers = (
        before_run_hook_manager,
        after_run_hook_manager,
        before_model_hook_manager,
        after_model_hook_manager,
        before_tool_hook_manager,
        after_tool_hook_manager,
    )

    assert len({id(manager) for manager in managers}) == 6
    with pytest.raises(TypeError):
        HookManager()
    assert before_tool_hook_manager.operations[0] is sandbox_operation
    assert sandbox_operation not in before_tool_hook_manager.operations[1:]


def test_sequential_manager_runs_fifo_and_rejection_short_circuits() -> None:
    manager = SequentialHookManager("before", "run")
    calls: list[str] = []
    manager.register(lambda _context: calls.append("first") or HookOperationResult.continue_execution({"a": 1}))
    manager.register(lambda _context: calls.append("second") or HookOperationResult.reject("blocked", {"b": 2}))
    manager.register(lambda _context: calls.append("third") or HookOperationResult.continue_execution())

    result = manager.execute(RunHookContext(RunHookInfo("session", "run", "task", "agent")))

    assert calls == ["first", "second"]
    assert result.decision == "reject"
    assert result.reason == "blocked"
    assert dict(result.data) == {"a": 1, "b": 2}


def test_sequential_manager_converts_operation_error_and_short_circuits() -> None:
    manager = SequentialHookManager("before", "model")
    calls: list[str] = []
    events = []

    def broken(_context):
        calls.append("broken")
        raise ValueError("boom")

    manager.register(broken)
    manager.register(lambda _context: calls.append("late") or HookOperationResult.continue_execution())

    with pytest.raises(HookExecutionError) as exc_info:
        manager.execute(object(), events.append)

    assert calls == ["broken"]
    assert exc_info.value.lifecycle == "model"
    assert exc_info.value.phase == "before"
    assert [event.kind for event in events] == ["hook_started", "hook_failed"]


class _OneToolPlanner:
    def __init__(self) -> None:
        self.calls = 0

    def decide(self, _runtime):
        self.calls += 1
        if self.calls == 1:
            return AssistantMessage(
                tool_messages=[
                    ToolMessage(
                        name="echo",
                        call_id="call_echo",
                        arguments={"value": "original", "metadata": {"source": "model"}},
                    )
                ]
            )
        return AssistantMessage(content="done")


def test_run_and_tool_managers_fire_at_boundaries_without_replacing_arguments(monkeypatch) -> None:
    observed: list[tuple[str, object]] = []

    def before_run(context):
        observed.append(("before_run", context.outcome))
        return HookOperationResult.continue_execution()

    def after_run(context):
        observed.append(("after_run", context.outcome.status if context.outcome else None))
        return HookOperationResult.continue_execution()

    def inspect_arguments(context):
        observed.append(("before_tool", context.arguments["value"]))
        with pytest.raises(TypeError):
            context.arguments["value"] = "replaced"
        context.arguments["metadata"]["source"] = "hook-copy"
        return HookOperationResult.continue_execution()

    def after_tool(context):
        observed.append(("after_tool", context.outcome.status if context.outcome else None))
        return HookOperationResult.continue_execution()

    monkeypatch.setattr(before_run_hook_manager, "_operations", [before_run])
    monkeypatch.setattr(after_run_hook_manager, "_operations", [after_run])
    monkeypatch.setattr(before_tool_hook_manager, "_operations", [sandbox_operation, inspect_arguments])
    monkeypatch.setattr(after_tool_hook_manager, "_operations", [after_tool])
    received: list[tuple[str, str]] = []

    def echo(value, metadata):
        received.append((value, metadata["source"]))
        return value

    tools = ToolRegistry([Tool("echo", "echo", echo)])
    runner = AgentRunner(_OneToolPlanner(), tools)

    result = runner.run(runner.new_runtime(task="call echo"))

    assert result.status == "completed"
    assert received == [("original", "model")]
    assert observed == [
        ("before_run", None),
        ("before_tool", "original"),
        ("after_tool", "succeeded"),
        ("after_run", "succeeded"),
    ]


class _CompletionClient:
    def run(self, _runtime) -> PreparedResponse:
        return PreparedResponse(AssistantMessage(content="model result"))


def test_model_managers_receive_before_context_and_after_outcome(monkeypatch) -> None:
    observed: list[tuple[str, object]] = []
    monkeypatch.setattr(
        before_model_hook_manager,
        "_operations",
        [lambda context: observed.append(("before", context.outcome)) or HookOperationResult.continue_execution()],
    )
    monkeypatch.setattr(
        after_model_hook_manager,
        "_operations",
        [
            lambda context: (
                observed.append(("after", context.outcome.status if context.outcome else None))
                or HookOperationResult.continue_execution()
            )
        ],
    )
    runtime = AgentRunner(_OneToolPlanner(), ToolRegistry()).new_runtime(task="model request")

    result = ModelRequestExecutor(_CompletionClient()).run(
        runtime,
        [UserMessage(content="hello")],
        operation="decision",
        output_mode="text",
    )

    assert result.message.content == "model result"
    assert observed == [("before", None), ("after", "succeeded")]


@pytest.mark.parametrize("tool_name", ["web_search", "web_fetch", "mcp_demo_echo"])
def test_external_tool_still_requires_approval_in_workspace_write(tool_name: str) -> None:
    calls: list[str] = []
    approvals = []

    class ExternalToolPlanner:
        def __init__(self) -> None:
            self.calls = 0

        def decide(self, _runtime):
            self.calls += 1
            if self.calls == 1:
                return AssistantMessage(
                    tool_messages=[
                        ToolMessage(
                            name=tool_name,
                            call_id="call_external",
                            arguments={"value": "ok"},
                        )
                    ]
                )
            return AssistantMessage(content="done")

    tools = ToolRegistry(
        [
            Tool(
                tool_name,
                "External tool",
                lambda value: calls.append(value) or value,
                requires_confirmation=True,
                read_only=tool_name.startswith("web_"),
            )
        ]
    )
    runner = AgentRunner(ExternalToolPlanner(), tools)
    runtime = runner.new_runtime(
        task="call external tool",
        interrupt=lambda request: approvals.append(request) or InterruptDecision("continue"),
    )
    runtime.state.permission_mode = "workspace_write"

    result = runner.run(runtime)

    assert result.status == "completed"
    assert calls == ["ok"]
    assert approvals[0].data["tool"] == tool_name


@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    [
        ("create_directory", {"path": "created/nested"}),
        ("write_file", {"path": "written.txt", "content": "created"}),
        ("edit_file", {"path": "edited.txt", "old_text": "before", "new_text": "after"}),
    ],
)
@pytest.mark.parametrize(
    ("permission_mode", "expected_approvals"),
    [("read_only", 1), ("workspace_write", 0), ("full_access", 0)],
)
def test_workspace_file_mutation_approval_matrix_executes_real_handler(
    tmp_path: Path,
    tool_name: str,
    arguments: dict[str, object],
    permission_mode: str,
    expected_approvals: int,
) -> None:
    if tool_name == "edit_file":
        (tmp_path / "edited.txt").write_text("before", encoding="utf-8")

    class FileToolPlanner:
        def __init__(self) -> None:
            self.calls = 0

        def decide(self, _runtime):
            self.calls += 1
            if self.calls == 1:
                return AssistantMessage(
                    tool_messages=[ToolMessage(name=tool_name, call_id="call_file", arguments=arguments)]
                )
            return AssistantMessage(content="done")

    approvals = []
    events = []
    runner = AgentRunner(FileToolPlanner(), ToolRegistry(tmp_path), workspace_root=str(tmp_path))
    runtime = runner.new_runtime(
        task="mutate workspace",
        on_event=events.append,
        interrupt=lambda request: approvals.append(request) or InterruptDecision("continue"),
    )
    runtime.state.permission_mode = permission_mode

    result = runner.run(runtime)

    assert result.status == "completed"
    assert len(approvals) == expected_approvals
    assert [event.kind for event in events].count("approval_requested") == expected_approvals
    if tool_name == "create_directory":
        assert (tmp_path / "created" / "nested").is_dir()
    elif tool_name == "write_file":
        assert (tmp_path / "written.txt").read_text(encoding="utf-8") == "created"
    else:
        assert (tmp_path / "edited.txt").read_text(encoding="utf-8") == "after"


@pytest.mark.parametrize(
    ("permission_mode", "expected_approvals"),
    [("read_only", 1), ("workspace_write", 1), ("full_access", 0)],
)
def test_approved_command_uses_hook_decision_for_real_process_and_cleans_up(
    tmp_path: Path, permission_mode: str, expected_approvals: int
) -> None:
    class RecordingLauncher(SandboxLauncher):
        def __init__(self) -> None:
            super().__init__(is_windows=os.name == "nt", allow_local_backend=True, environment=os.environ)
            self.policies = []

        def launch(self, argv, policy, **kwargs):
            self.policies.append(policy)
            return super().launch(argv, policy, **kwargs)

    launcher = RecordingLauncher()
    requests = []
    planner = _OneToolPlanner()
    decisions = 0

    def decide(_runtime):
        nonlocal decisions
        decisions += 1
        if decisions > 1:
            return AssistantMessage(content="done")
        return AssistantMessage(
            tool_messages=[
                ToolMessage(
                    name="run_command",
                    call_id="call_command",
                    arguments={
                        "command": (
                            'powershell -NoProfile -Command "Start-Sleep -Milliseconds 200; Write-Output hook-sandbox"'
                            if os.name == "nt"
                            else "sleep 0.2; printf hook-sandbox"
                        )
                    },
                )
            ]
        )

    planner.decide = decide
    tools = ToolRegistry(tmp_path)
    runner = AgentRunner(
        planner,
        tools,
        workspace_root=str(tmp_path),
        sandbox_launcher=launcher,
        sandbox_config={},
    )
    runtime = runner.new_runtime(
        task="run approved command",
        interrupt=lambda request: requests.append(request) or InterruptDecision("continue"),
    )
    runtime.state.permission_mode = permission_mode

    result = runner.run(runtime)

    command_message = next(
        message for message in result.history if isinstance(message, AssistantMessage) and message.tool_messages
    )
    assert command_message.tool_messages[0].status == "succeeded"
    assert "hook-sandbox" in (command_message.tool_messages[0].content or "")
    assert len(requests) == expected_approvals
    if requests:
        assert requests[0].data["permission_target"] == permission_mode
        assert requests[0].data["network_target"]["mode"] == "no_network"
    assert launcher.policies[0].file_mode is PermissionMode(permission_mode)
    assert launcher.policies[0].network_mode is NetworkMode.NO_NETWORK
    assert launcher._temp_dirs == {}


def test_command_without_sandbox_launcher_is_rejected_before_handler() -> None:
    calls: list[str] = []

    class CommandPlanner:
        def __init__(self) -> None:
            self.calls = 0

        def decide(self, _runtime):
            self.calls += 1
            if self.calls == 1:
                return AssistantMessage(
                    tool_messages=[
                        ToolMessage(
                            name="run_command",
                            call_id="call_without_sandbox",
                            arguments={"command": "must-not-run"},
                        )
                    ]
                )
            return AssistantMessage(content="done")

    tools = ToolRegistry(
        [
            Tool(
                "run_command",
                "command",
                lambda command: calls.append(command) or "unexpected",
                parameters={
                    "type": "object",
                    "properties": {"command": {"type": "string"}},
                    "required": ["command"],
                },
                read_only=False,
            )
        ]
    )
    runner = AgentRunner(CommandPlanner(), tools)

    result = runner.run(runner.new_runtime(task="run command without sandbox"))

    assert calls == []
    assert result.actions[0].status == "failed"
    assert "Sandbox runtime is not healthy" in (result.actions[0].content or "")


def test_real_sandbox_command_timeout_cleans_process_resources(tmp_path: Path) -> None:
    launcher = SandboxLauncher(is_windows=os.name == "nt", allow_local_backend=True, environment=os.environ)
    decision = SandboxExecutionDecision(
        launcher=launcher,
        workspace=tmp_path,
        session_id="session-timeout",
        user_id="local",
        file_mode=PermissionMode.READ_ONLY,
        network_mode=NetworkMode.NO_NETWORK,
        network_allowlist=(),
        proxy_port=17831,
        limits=SandboxLimits(wall_seconds=5),
    )
    command = WorkspaceCommand(tmp_path)
    slow_command = "powershell -NoProfile -Command Start-Sleep -Seconds 5" if os.name == "nt" else "sleep 5"

    with pytest.raises(ToolError, match="timed out"):
        command.run_with_context(
            ToolInvocationContext(session_id="session-timeout", sandbox_decision=decision),
            slow_command,
            timeout_seconds=1,
        )

    assert launcher._temp_dirs == {}
