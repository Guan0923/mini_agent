from __future__ import annotations

import json
from pathlib import Path
from threading import Event, Lock, Thread, get_ident
from time import sleep
from types import SimpleNamespace

import pytest

from backend.configuration import ClientPaths, ConfigurationError
from backend.domain import RunState
from backend.mcp import client as mcp_client
from backend.mcp.config import McpSettings, McpTrustStore, prepare_mcp_plan
from backend.runtime import AgentRunner
from backend.runtime.application import factory as app_factory
from backend.runtime.capability_settings import SubagentSettings
from backend.runtime.core.context import AgentRuntime
from backend.runtime.subagent_bridge import ParentRuntimeBridge
from backend.runtime.subagents import SubagentCoordinator
from backend.sandbox import BrokerStatus, SandboxLauncher
from backend.tools import ToolError, ToolRegistry
from backend.tools.filesystem import normalized_workspace_path


def _write_mcp(path: Path, *, secret: str = "secret-one", tool_name: str = "echo", plaintext: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    env_line = f'env = {{ API_TOKEN = "{secret}" }}' if plaintext else 'env_refs = { API_TOKEN = "env://API_TOKEN" }'
    path.write_text(
        "\n".join(
            (
                "[servers.demo]",
                'command = "demo-server"',
                f'args = ["--tool", "{tool_name}"]',
                env_line,
            )
        ),
        encoding="utf-8",
    )


def test_user_mcp_plaintext_secrets_are_rejected(tmp_path: Path) -> None:
    paths = ClientPaths(tmp_path / "home")
    _write_mcp(paths.mcp_file)

    with pytest.raises(ToolError, match="plaintext"):
        prepare_mcp_plan(paths)


def test_project_mcp_file_is_ignored(tmp_path: Path) -> None:
    paths = ClientPaths(tmp_path / "home")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _write_mcp(workspace / ".mini_agent" / "mcp.toml")

    plan = prepare_mcp_plan(paths, workspace)

    assert plan.effective_servers() == ()


@pytest.mark.parametrize("value", [0, -1, float("inf"), "10", True])
def test_mcp_timeouts_require_finite_positive_numbers(value: object) -> None:
    with pytest.raises(ConfigurationError, match="finite positive"):
        McpSettings.from_config({"mcp": {"call_timeout_seconds": value}})


def test_user_mcp_servers_are_started_and_project_file_is_ignored(tmp_path: Path, monkeypatch) -> None:
    paths = ClientPaths(tmp_path / "home")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    started: list[tuple] = []

    def start(configs, _settings, **_kwargs):
        started.append(configs)
        return mcp_client.ExternalMcpResources()

    monkeypatch.setattr(app_factory, "start_external_tools", start)
    _write_mcp(workspace / ".mini_agent" / "mcp.toml")

    app_factory._external_resources(paths, {})
    assert started == [()]

    _write_mcp(paths.mcp_file, plaintext=False)
    app_factory._external_resources(
        paths,
        {},
    )
    assert len(started) == 2
    assert len(started[1]) == 1
    assert started[1][0].name == "demo"


def test_external_mcp_server_starts_without_command_sandbox(tmp_path: Path, monkeypatch) -> None:
    paths = ClientPaths(tmp_path / "home")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _write_mcp(paths.mcp_file, plaintext=False)
    started = []
    monkeypatch.setattr(app_factory, "start_external_tools", lambda configs, *_args, **_kwargs: started.extend(configs))

    app_factory._external_resources(paths, {})

    assert [server.name for server in started] == ["demo"]


def test_sandbox_runtime_only_disables_run_command_when_broker_is_unhealthy(monkeypatch) -> None:
    class Broker:
        def __init__(self, status: BrokerStatus) -> None:
            self._status = status

        def status(self) -> BrokerStatus:
            return self._status

    unavailable = Broker(BrokerStatus(installed=False, healthy=False))
    monkeypatch.setattr(app_factory.WindowsBrokerClient, "from_system", lambda **_kwargs: unavailable)
    launcher, _ = app_factory._sandbox_runtime({})
    assert launcher is None

    unhealthy = Broker(BrokerStatus(installed=True, healthy=False))
    monkeypatch.setattr(app_factory.WindowsBrokerClient, "from_system", lambda **_kwargs: unhealthy)
    launcher, _ = app_factory._sandbox_runtime({})
    assert launcher is None

    healthy = Broker(BrokerStatus(installed=True, healthy=True))
    monkeypatch.setattr(app_factory.WindowsBrokerClient, "from_system", lambda **_kwargs: healthy)
    launcher, config = app_factory._sandbox_runtime({"sandbox_config": {"policy_version": 2, "enabled": False}})

    assert isinstance(launcher, SandboxLauncher)
    assert "enabled" not in config
    assert "file_mode" not in config
    assert config["policy_version"] == 3


def test_mcp_trust_store_is_compat_noop(tmp_path: Path) -> None:
    paths = ClientPaths(tmp_path / "home")
    plan = prepare_mcp_plan(paths)
    store = McpTrustStore(paths.mcp_trust_file)

    assert store.is_trusted(plan) is True
    store.trust(plan)
    assert paths.mcp_trust_file.exists() is False


def test_invalid_external_tool_name_closes_its_manager(monkeypatch) -> None:
    closed = False

    class Manager:
        def __init__(self, _configs) -> None:
            self.definitions = {"demo": [SimpleNamespace(name="bad.name", inputSchema={})]}

        def close(self) -> None:
            nonlocal closed
            closed = True

    monkeypatch.setattr(mcp_client, "ExternalMcpManager", Manager)
    config = mcp_client.McpServerConfig("demo", "demo-server")

    with pytest.raises(ToolError, match="MCP tool name"):
        mcp_client.start_external_tools((config,))

    assert closed is True


def test_mcp_initialization_timeout_has_stable_error(monkeypatch) -> None:
    class TimeoutManager:
        def __init__(self, _configs) -> None:
            raise mcp_client.FutureTimeoutError

    monkeypatch.setattr(mcp_client, "ExternalMcpManager", TimeoutManager)
    config = mcp_client.McpServerConfig("demo", "demo-server")

    with pytest.raises(ToolError, match="initialization timed out"):
        mcp_client.start_external_tools((config,))


def test_mcp_call_timeout_has_stable_error(monkeypatch) -> None:
    manager = object.__new__(mcp_client.ExternalMcpManager)
    manager._settings = McpSettings(call_timeout_seconds=0.01)
    manager._sessions = {"demo": SimpleNamespace(call_tool=lambda *_args: object())}

    def timeout(*_args, **_kwargs):
        raise mcp_client.FutureTimeoutError

    monkeypatch.setattr(manager, "_submit", timeout)

    with pytest.raises(ToolError, match="demo/echo timed out"):
        manager.call("demo", "echo", {})


def test_mcp_shutdown_timeout_stops_loop_and_has_stable_error(monkeypatch) -> None:
    class Loop:
        def __init__(self) -> None:
            self.running = True
            self.closed = False

        def is_running(self) -> bool:
            return self.running

        def stop(self) -> None:
            self.running = False

        def call_soon_threadsafe(self, callback) -> None:
            callback()

        def close(self) -> None:
            self.closed = True

    loop = Loop()
    manager = object.__new__(mcp_client.ExternalMcpManager)
    manager._closed = False
    manager._settings = McpSettings(shutdown_timeout_seconds=0.01)
    manager._loop = loop
    manager._stack = SimpleNamespace(aclose=lambda: object())
    manager._thread = SimpleNamespace(join=lambda **_kwargs: None, is_alive=lambda: False)

    def timeout(awaitable, **_kwargs):
        awaitable.close()
        raise mcp_client.FutureTimeoutError

    monkeypatch.setattr(manager, "_submit", timeout)

    with pytest.raises(ToolError, match="MCP shutdown timed out"):
        manager.close()

    assert loop.running is False
    assert loop.closed is True


def test_non_text_mcp_content_is_described_without_binary_payload() -> None:
    result = SimpleNamespace(
        content=[SimpleNamespace(type="image", mimeType="image/png", data="abc123")],
        structuredContent={"ok": True},
    )

    rendered = mcp_client._render_result(result)

    assert '"type": "image"' in rendered
    assert '"size": 6' in rendered
    assert "abc123" not in rendered
    assert '"ok": true' in rendered


def test_runner_closes_only_its_own_resources() -> None:
    first = SimpleNamespace(closed=0)
    second = SimpleNamespace(closed=0)
    first.close = lambda: setattr(first, "closed", first.closed + 1)
    second.close = lambda: setattr(second, "closed", second.closed + 1)
    runner_one = AgentRunner(object(), ToolRegistry(), resources=(first,))
    runner_two = AgentRunner(object(), ToolRegistry(), resources=(second,))

    runner_one.close()
    runner_one.close()

    assert first.closed == 1
    assert second.closed == 0
    runner_two.close()
    assert second.closed == 1


def test_closed_parent_bridge_rejects_new_worker_messages() -> None:
    bridge = ParentRuntimeBridge(lambda *_args, **_kwargs: None, lambda _request: None)
    bridge.close()

    with pytest.raises(RuntimeError, match="closed"):
        bridge.event("late", "late worker event")


def test_parent_bridge_applies_backpressure_without_losing_events() -> None:
    handled: list[str] = []
    bridge = ParentRuntimeBridge(
        lambda _kind, message, **_data: handled.append(message),
        lambda _request: None,
        capacity=1,
    )
    bridge.event("event", "first")
    finished = Event()

    def produce() -> None:
        bridge.event("event", "second")
        finished.set()

    worker = Thread(target=produce)
    worker.start()
    assert finished.wait(0.02) is False

    assert bridge.drain() == 1
    worker.join(timeout=1.0)
    assert finished.is_set() is True
    assert bridge.drain() == 1
    bridge.close()

    assert handled == ["first", "second"]


class _Tools:
    def names(self):
        return []

    def read_only_names(self):
        return []

    def specs(self):
        return []

    def read_only_specs(self):
        return []

    def is_read_only(self, _name):
        return False

    def requires_confirmation(self, _name):
        return False

    def is_retryable(self, _name):
        return False

    def validate_arguments(self, _name, _arguments):
        return None

    def invoke(self, _name, _arguments, confirmed=False):
        return "ok"


class _Child:
    def __init__(self, fail: bool = False) -> None:
        self.tools = _Tools()
        self.fail = fail
        self.task = ""

    def new_runtime(self, *, task: str, session_id: str | None = None, **_kwargs):
        self.task = task
        return AgentRuntime.ephemeral(session_id=session_id or "child", planner=object(), tools=self.tools)

    def run(self, _runtime):
        if self.fail and self.task == "fail":
            raise ValueError("API_TOKEN=should-not-leak " + "x" * 3_000)
        sleep(0.01)
        return SimpleNamespace(status="completed", final_answer=self.task)


def _parent() -> AgentRuntime:
    runtime = AgentRuntime.ephemeral(session_id="parent", planner=object(), tools=_Tools())
    runtime.state.current_run = RunState(task="parent", mode="agent")
    return runtime


def test_subagent_parent_publication_is_serial_and_batch_failure_is_truthful() -> None:
    runtime = _parent()
    guard = Lock()
    active = 0
    maximum = 0
    owner_threads: set[int] = set()

    def publish(_event) -> None:
        nonlocal active, maximum
        with guard:
            active += 1
            maximum = max(maximum, active)
            owner_threads.add(get_ident())
        sleep(0.002)
        with guard:
            active -= 1

    runtime.services.publish = publish
    coordinator = SubagentCoordinator(lambda: _Child(fail=True))
    payload = json.loads(
        coordinator.invoke(
            runtime,
            "delegate_tasks",
            {"tasks": [{"id": str(index), "task": "fail" if index == 2 else "ok"} for index in range(6)]},
        )
    )

    assert maximum == 1
    assert len(owner_threads) == 1
    assert payload["status"] == "failed"
    failure = payload["results"][2]
    assert failure["status"] == "failed"
    assert len(failure["error"]) <= 2_100
    assert "should-not-leak" not in failure["error"]


def test_subagent_approval_runs_on_parent_invocation_thread() -> None:
    parent_thread = get_ident()
    approval_threads: list[int] = []
    runtime = _parent()

    class ApprovalChild(_Child):
        def new_runtime(self, *, task: str, session_id: str | None = None, interrupt=None, **_kwargs):
            child = super().new_runtime(task=task, session_id=session_id)
            child.services.interrupt = interrupt
            return child

        def run(self, child_runtime):
            assert child_runtime.services.interrupt is not None
            child_runtime.services.interrupt(
                SimpleNamespace(kind="tool", message="approve", data={"tool": "write_file"}, questions=())
            )
            return SimpleNamespace(status="completed", final_answer="approved")

    runtime.services.interrupt = lambda _request: (
        approval_threads.append(get_ident()) or SimpleNamespace(action="continue")
    )
    SubagentCoordinator(ApprovalChild).invoke(
        runtime,
        "delegate_tasks",
        {"tasks": [{"id": "one", "task": "approve"}]},
    )

    assert approval_threads == [parent_thread]


def test_subagent_approval_wait_does_not_consume_batch_timeout() -> None:
    runtime = _parent()

    class ApprovalChild(_Child):
        def new_runtime(self, *, task: str, session_id: str | None = None, interrupt=None, **_kwargs):
            child = super().new_runtime(task=task, session_id=session_id)
            child.services.interrupt = interrupt
            return child

        def run(self, child_runtime):
            assert child_runtime.services.interrupt is not None
            child_runtime.services.interrupt(
                SimpleNamespace(kind="tool", message="approve", data={"tool": "write_file"}, questions=())
            )
            return SimpleNamespace(status="completed", final_answer="approved")

    def approve(_request):
        sleep(0.1)
        return SimpleNamespace(action="continue")

    runtime.services.interrupt = approve
    settings = SubagentSettings(1, 1, 1.0, 0.05)
    payload = json.loads(
        SubagentCoordinator(ApprovalChild, settings=settings).invoke(
            runtime,
            "delegate_tasks",
            {"tasks": [{"id": "one", "task": "approve"}]},
        )
    )

    assert payload["status"] == "completed"


def test_subagent_workers_use_local_cancellation_and_close_child_runner() -> None:
    parent_thread = get_ident()
    cancellation_threads: list[int] = []
    created: list[object] = []
    runtime = _parent()

    class CancellationChild(_Child):
        def __init__(self) -> None:
            super().__init__()
            self.closed = False
            created.append(self)

        def run(self, child_runtime):
            assert child_runtime.services.cancel_requested is not None
            child_runtime.services.cancel_requested()
            return SimpleNamespace(status="completed", final_answer="done")

        def close(self) -> None:
            self.closed = True

    runtime.services.cancel_requested = lambda: cancellation_threads.append(get_ident()) or False
    payload = json.loads(
        SubagentCoordinator(CancellationChild).invoke(
            runtime,
            "delegate_tasks",
            {"tasks": [{"id": "one", "task": "check cancellation"}]},
        )
    )

    assert payload["status"] == "completed"
    assert cancellation_threads
    assert set(cancellation_threads) == {parent_thread}
    assert len(created) == 1
    assert created[0].closed is True


def test_subagent_does_not_swallow_base_exceptions() -> None:
    class InterruptingChild(_Child):
        def run(self, _runtime):
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        SubagentCoordinator(InterruptingChild).invoke(
            _parent(),
            "delegate_tasks",
            {"tasks": [{"id": "one", "task": "interrupt"}]},
        )


def test_subagent_limits_and_cooperative_timeout() -> None:
    class SlowChild(_Child):
        def run(self, _runtime):
            sleep(0.05)
            return SimpleNamespace(status="completed", final_answer=self.task)

    settings = SubagentSettings(1, 1, 0.01, 0.05)
    coordinator = SubagentCoordinator(SlowChild, settings=settings)

    with pytest.raises(ToolError, match="at most 1"):
        coordinator.invoke(
            _parent(),
            "delegate_tasks",
            {"tasks": [{"id": "one", "task": "one"}, {"id": "two", "task": "two"}]},
        )

    payload = json.loads(coordinator.invoke(_parent(), "delegate_tasks", {"tasks": [{"id": "one", "task": "slow"}]}))
    assert payload["status"] == "failed"
    assert payload["results"][0]["status"] == "timed_out"


def test_workspace_lock_path_normalization_rejects_alias_escape(tmp_path: Path) -> None:
    direct = normalized_workspace_path(tmp_path, "file.txt")
    assert normalized_workspace_path(tmp_path, "folder/../file.txt") == direct
    assert normalized_workspace_path(tmp_path, ".\\file.txt") == direct
    with pytest.raises(ToolError, match="stay inside"):
        normalized_workspace_path(tmp_path, "../file.txt")
