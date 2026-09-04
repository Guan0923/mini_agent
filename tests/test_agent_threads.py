from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Event, Thread
from time import monotonic, sleep
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from redis import Redis

from backend.api import agent_thread_stream
from backend.api.agent_thread_stream import AgentThreadEventHub
from backend.api.app import create_app
from backend.api.chat import routes as chat_routes
from backend.api.state import WebAppState
from backend.configuration import ClientPaths
from backend.domain import (
    CHECKPOINT_PREAMBLE,
    AssistantMessage,
    MessageEnvelope,
    MessageQueueUnavailable,
    RuntimeThread,
    ThreadContext,
    ThreadNode,
    ToolMessage,
)
from backend.domain.runtime_state import (
    NodeFrame,
    NodeWriter,
    RuntimeState,
    new_node_id,
    new_thread_id,
    utc_iso,
)
from backend.jobs import JobRegistry
from backend.planning.context_management import ContextCompactionResult
from backend.providers import ModelConfig
from backend.runtime.agent_thread_index import AgentThreadIndex
from backend.runtime.application.factory import build_application
from backend.runtime.capability_settings import SubagentSettings
from backend.runtime.execution.runner import AgentRunner
from backend.runtime.subagents import SubagentCoordinator
from backend.storage.message_queue import MemoryMessageQueue, RedisMessageQueue
from backend.storage.sqlite import SQLiteSessionStore
from backend.storage.sqlite_agent_threads import AgentThreadCreate
from backend.tools import Tool, ToolError, ToolRegistry, delegation_tools


class _AnswerPlanner:
    name = "local-answer"

    def decide(self, runtime):
        return AssistantMessage(content=f"done:{runtime.run.task}")


class _DelegatingRootPlanner:
    name = "local-delegating-root"

    def __init__(self) -> None:
        self.calls = 0

    def decide(self, runtime):
        self.calls += 1
        delegated_paths: set[str] = set()
        for message in runtime.model_messages():
            if not isinstance(message, AssistantMessage):
                continue
            for tool in message.tool_messages:
                if tool.name != "delegate_tasks" or not tool.content:
                    continue
                try:
                    result = json.loads(tool.content)
                except json.JSONDecodeError:
                    continue
                if isinstance(result, dict) and result.get("thread_path"):
                    delegated_paths.add(str(result["thread_path"]))
        pending = next(
            (
                (name, task)
                for name, task in (("one", "first"), ("two", "second"))
                if f"/root/{name}" not in delegated_paths
            ),
            None,
        )
        if pending is not None:
            name, task = pending
            return AssistantMessage(
                tool_messages=[
                    ToolMessage(
                        name="delegate_tasks",
                        call_id=f"delegate_http_{name}",
                        arguments={
                            "subagent_path": f"/root/{name}",
                            "subagent_task": task,
                            "context_transfer_strategy": "independent",
                        },
                    )
                ]
            )
        if not any(
            tool.name == "get_thread_node" and tool.content
            for message in runtime.model_messages()
            if isinstance(message, AssistantMessage)
            for tool in message.tool_messages
        ):
            return AssistantMessage(
                tool_messages=[ToolMessage(name="get_thread_node", call_id="inspect_agents", arguments={})]
            )
        return AssistantMessage(content="root delegated both Agents")


@pytest.fixture
def local_subagent_model() -> Generator[tuple[ModelConfig, list[str]], None, None]:
    model_calls: list[str] = []

    class LocalModelHandler(BaseHTTPRequestHandler):
        def log_message(self, _format: str, *_args: object) -> None:
            return

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length) or b"{}")
            if self.path != "/v1/chat/completions":
                self.send_error(404)
                return
            requested_model = str(payload.get("model") or "")
            model_calls.append(requested_model)
            if requested_model == "unknown":
                body = json.dumps(
                    {
                        "message": 'Model "unknown" is not supported by any configured account in this group',
                        "type": "model_not_found",
                    }
                ).encode()
                self.send_response(404)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if payload.get("stream"):
                events = [
                    {
                        "id": "subagent-trace-test",
                        "object": "chat.completion.chunk",
                        "created": 1,
                        "model": "subagent-trace-test",
                        "choices": [
                            {
                                "index": 0,
                                "delta": {
                                    "role": "assistant",
                                    "content": "child answered through local HTTP",
                                },
                                "finish_reason": "stop",
                            }
                        ],
                        "usage": None,
                    },
                    {"choices": [], "usage": {"input_tokens": 4, "output_tokens": 3, "total_tokens": 7}},
                ]
                body = ("".join(f"data: {json.dumps(event)}\n\n" for event in events) + "data: [DONE]\n\n").encode()
                content_type = "text/event-stream"
            else:
                body = json.dumps(
                    {
                        "id": "subagent-trace-test",
                        "object": "chat.completion",
                        "created": 1,
                        "model": "subagent-trace-test",
                        "choices": [
                            {
                                "index": 0,
                                "message": {
                                    "role": "assistant",
                                    "content": "child answered through local HTTP",
                                },
                                "finish_reason": "stop",
                            }
                        ],
                        "usage": {"input_tokens": 4, "output_tokens": 3, "total_tokens": 7},
                    }
                ).encode()
                content_type = "application/json"
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), LocalModelHandler)
    thread = Thread(target=server.serve_forever, name="subagent-trace-model", daemon=True)
    thread.start()
    try:
        port = int(server.server_address[1])
        yield (
            ModelConfig(
                "local-test-key",
                f"http://127.0.0.1:{port}/v1",
                "subagent-trace-test",
                max_tokens=256,
                context_size=128_000,
                provider_name="subagent-trace-local",
            ),
            model_calls,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _finished_source(store: SQLiteSessionStore, session_id: str, *, turn_id: str = "turn_source") -> RuntimeState:
    root = store.ensure_root_node(session_id, id="turn_root")
    writer = NodeWriter(store)
    source = writer.create(
        RuntimeState.create(
            session_id=session_id,
            thread_id=session_id,
            id=turn_id,
            parent=root,
            user_content="parent task",
        )
    )
    return writer.finalize(source, "success")


def _agent_create(
    session_id: str,
    parent: RuntimeState,
    *,
    name: str,
    root_thread_id: str | None = None,
) -> AgentThreadCreate:
    timestamp = utc_iso()
    thread_id = new_thread_id()
    turn = RuntimeState.create(
        session_id=session_id,
        thread_id=thread_id,
        id=new_node_id(),
        parent=parent,
        user_content=f"task:{name}",
    )
    return AgentThreadCreate(
        RuntimeThread(session_id, thread_id, "subagent", turn.id, turn.id, timestamp, timestamp),
        ThreadNode(
            session_id,
            thread_id,
            root_thread_id or session_id,
            parent.thread_id,
            f"/root/{name}",
            "running",
            1,
            timestamp,
            timestamp,
        ),
        ThreadContext(thread_id, "independent", "independent", parent.id, parent.current_data_idx),
        turn,
    )


def _nested_agent_create(
    session_id: str,
    parent: RuntimeState,
    *,
    name: str,
    parent_path: str,
    depth: int,
    root_thread_id: str | None = None,
) -> AgentThreadCreate:
    timestamp = utc_iso()
    thread_id = new_thread_id()
    turn = RuntimeState.create(
        session_id=session_id,
        thread_id=thread_id,
        id=new_node_id(),
        parent=parent,
        user_content=f"task:{name}",
    )
    return AgentThreadCreate(
        RuntimeThread(session_id, thread_id, "subagent", turn.id, turn.id, timestamp, timestamp),
        ThreadNode(
            session_id,
            thread_id,
            root_thread_id or session_id,
            parent.thread_id,
            f"{parent_path}/{name}",
            "running",
            depth,
            timestamp,
            timestamp,
        ),
        ThreadContext(thread_id, "independent", "independent", parent.id, parent.current_data_idx),
        turn,
    )


def test_agent_thread_index_rebuilds_and_tracks_committed_heads(tmp_path: Path) -> None:
    paths = ClientPaths(tmp_path / "data")
    index = AgentThreadIndex()
    store = SQLiteSessionStore(paths, index)
    session = store.create_session("index")
    source = _finished_source(store, session.session_id)
    child = _agent_create(session.session_id, source, name="worker")
    store.create_agent_thread(session.session_id, child)

    assert index.threads_for_session(session.session_id) == frozenset({session.session_id, child.node.thread_id})
    assert index.session_for_thread(child.node.thread_id) == session.session_id
    assert index.head_for_thread(child.node.thread_id) == child.turn.id
    assert index.thread_for_path(session.session_id, session.session_id, "/root/worker") == child.node.thread_id
    assert index.path_for_thread(child.node.thread_id) == "/root/worker"

    rebuilt = AgentThreadIndex()
    rebuilt.rebuild(SQLiteSessionStore(paths))
    assert rebuilt.threads_for_session(session.session_id) == index.threads_for_session(session.session_id)
    assert rebuilt.path_for_thread(child.node.thread_id) == "/root/worker"


def test_empty_session_creates_agent_root_only_with_its_first_turn(tmp_path: Path) -> None:
    store = SQLiteSessionStore(ClientPaths(tmp_path / "empty-root"))
    session = store.create_session("empty")
    assert store.get_thread_node(session.session_id, session.session_id) is None
    runtime_thread = store.get_runtime_thread(session.session_id, session.session_id)
    assert runtime_thread is not None and runtime_thread.current_turn_id is None

    source = _finished_source(store, session.session_id)
    root = store.get_thread_node(session.session_id, session.session_id)
    assert root is not None
    assert root.thread_path == "/root" and root.thread_status == "success"
    assert root.thread_id == source.thread_id == session.session_id


def test_agent_thread_event_hub_replays_latest_and_stays_open_across_turns(tmp_path: Path) -> None:
    store = SQLiteSessionStore(ClientPaths(tmp_path / "events"))
    session = store.create_session("events")
    source = _finished_source(store, session.session_id)
    child = _agent_create(session.session_id, source, name="worker")
    assert child.turn is not None
    hub = AgentThreadEventHub()

    first = hub.subscribe(session.session_id, child.node.thread_id)
    assert first.next_event()["type"] == "thread.ready"
    hub.start_turn(child.turn)
    assert first.next_event()["turn"]["id"] == child.turn.id
    finished = child.turn.clone()
    finished.status = "success"
    hub.finish_turn(child.node.thread_id, finished)
    assert first.next_event() == {
        "type": "turn.terminal",
        "session_id": session.session_id,
        "thread_id": child.node.thread_id,
        "turn_id": child.turn.id,
        "status": "success",
    }

    late = hub.subscribe(session.session_id, child.node.thread_id)
    assert late.next_event()["type"] == "thread.ready"
    assert late.next_event()["turn"]["status"] == "success"
    assert first.closed is False
    hub.close()
    assert first.closed is True and late.closed is True


def test_agent_thread_event_hub_rebases_each_subscriber_and_resets_for_new_turn(tmp_path: Path) -> None:
    store = SQLiteSessionStore(ClientPaths(tmp_path / "rebased-events"))
    session = store.create_session("rebased-events")
    source = _finished_source(store, session.session_id)
    child = _agent_create(session.session_id, source, name="worker")
    assert child.turn is not None
    hub = AgentThreadEventHub()

    early = hub.subscribe(session.session_id, child.node.thread_id)
    assert early.next_event()["type"] == "thread.ready"
    hub.start_turn(child.turn)
    assert early.next_event()["revision"] == 0

    current = child.turn.clone()
    for source_revision in range(1, 6):
        current.usage["output_tokens"] = source_revision
        hub.publish_frame(
            child.node.thread_id,
            NodeFrame(
                "turn.delta",
                current.session_id,
                current.id,
                source_revision,
                patch={"usage": dict(current.usage)},
            ),
            current,
        )
        assert early.next_event()["revision"] == source_revision

    late = hub.subscribe(session.session_id, child.node.thread_id)
    assert late.next_event()["type"] == "thread.ready"
    late_snapshot = late.next_event()
    assert late_snapshot["revision"] == 0
    assert late_snapshot["turn"]["usage"]["output_tokens"] == 5

    current.usage["output_tokens"] = 6
    hub.publish_frame(
        child.node.thread_id,
        NodeFrame(
            "turn.delta",
            current.session_id,
            current.id,
            6,
            patch={"usage": dict(current.usage)},
        ),
        current,
    )
    assert early.next_event()["revision"] == 6
    assert late.next_event()["revision"] == 1

    next_turn = RuntimeState.create(
        session_id=session.session_id,
        thread_id=child.node.thread_id,
        parent=current,
        user_content="next task",
    )
    hub.start_turn(next_turn)
    assert early.next_event()["revision"] == 0
    assert late.next_event()["revision"] == 0
    hub.publish_frame(
        child.node.thread_id,
        NodeFrame(
            "turn.delta",
            next_turn.session_id,
            next_turn.id,
            1,
            patch={"status": "success"},
        ),
        next_turn,
    )
    assert early.next_event()["revision"] == 1
    assert late.next_event()["revision"] == 1


def test_agent_thread_event_hub_snapshots_first_delta_for_unseeded_subscriber() -> None:
    hub = AgentThreadEventHub()
    subscription = hub.subscribe("session_1", "session_1")
    assert subscription.next_event()["type"] == "thread.ready"
    current = RuntimeState.create(
        session_id="session_1",
        thread_id="session_1",
        id="turn_1",
        user_content="task",
    )

    hub.publish_frame(
        current.thread_id,
        NodeFrame("turn.delta", current.session_id, current.id, 5, patch={"status": "running"}),
        current,
    )
    assert subscription.next_event()["revision"] == 0
    hub.publish_frame(
        current.thread_id,
        NodeFrame("turn.delta", current.session_id, current.id, 6, patch={"status": "success"}),
        current,
    )
    assert subscription.next_event()["revision"] == 1


def test_agent_thread_subscription_emits_heartbeat_without_closing(monkeypatch: pytest.MonkeyPatch) -> None:
    ticks = iter((0.0, 16.0))
    monkeypatch.setattr(agent_thread_stream, "monotonic", lambda: next(ticks))
    hub = AgentThreadEventHub()
    subscription = hub.subscribe("session_1", "thread_1")

    async def read_events() -> tuple[str, str]:
        stream = subscription.as_sse()
        ready = await anext(stream)
        heartbeat = await anext(stream)
        await stream.aclose()
        return ready, heartbeat

    ready, heartbeat = asyncio.run(read_events())
    assert '"type":"thread.ready"' in ready
    assert heartbeat == ": heartbeat\n\n"
    assert subscription.closed is True


def test_agent_thread_http_navigation_and_root_mediated_message(tmp_path: Path) -> None:
    queue = MemoryMessageQueue()
    state = WebAppState(tmp_path / "web-agent-api", message_queue=queue)
    try:
        with TestClient(create_app(state)) as client:
            sidebar = client.post("/api/sidebar-threads", json={}).json()
            store = SQLiteSessionStore(state.paths, state.agent_thread_index)
            source = _finished_source(store, sidebar["session_id"])
            child = _agent_create(sidebar["session_id"], source, name="worker")
            store.create_agent_thread(sidebar["session_id"], child)
            grandchild = _nested_agent_create(
                sidebar["session_id"],
                child.turn,
                name="nested",
                parent_path=child.node.thread_path,
                depth=2,
            )
            store.create_agent_thread(sidebar["session_id"], grandchild)

            fork_response = client.post(f"/api/turns/{source.id}/fork", json={})
            assert fork_response.status_code == 201, fork_response.text
            fork_payload = fork_response.json()
            fork_turn = RuntimeState.from_dict(fork_payload["turn"])
            fork_thread_id = str(fork_payload["sidebar_thread"]["thread_id"])
            assert fork_turn.thread_id == fork_thread_id
            assert fork_turn.data == source.data
            fork_root = store.get_thread_node(sidebar["session_id"], fork_thread_id)
            assert fork_root is not None and fork_root.root_thread_id == fork_thread_id
            assert (
                client.get(
                    f"/api/agent-threads/{fork_thread_id}/children",
                    params={"session_id": sidebar["session_id"]},
                ).json()
                == []
            )
            fork_child = _agent_create(
                sidebar["session_id"],
                fork_turn,
                name="worker",
                root_thread_id=fork_thread_id,
            )
            store.create_agent_thread(sidebar["session_id"], fork_child)

            children = client.get(
                f"/api/agent-threads/{sidebar['session_id']}/children",
                params={"session_id": sidebar["session_id"]},
            )
            assert children.status_code == 200
            assert children.json() == [
                {
                    "thread_id": child.node.thread_id,
                    "thread_path": "/root/worker",
                    "thread_status": "running",
                    "task_result": "",
                }
            ]
            fork_children = client.get(
                f"/api/agent-threads/{fork_thread_id}/children",
                params={"session_id": sidebar["session_id"]},
            )
            assert fork_children.status_code == 200
            assert fork_children.json() == [
                {
                    "thread_id": fork_child.node.thread_id,
                    "thread_path": "/root/worker",
                    "thread_status": "running",
                    "task_result": "",
                }
            ]
            nested = client.get(
                f"/api/agent-threads/{child.node.thread_id}/children",
                params={"session_id": sidebar["session_id"]},
            )
            assert nested.status_code == 200
            assert nested.json() == [
                {
                    "thread_id": grandchild.node.thread_id,
                    "thread_path": "/root/worker/nested",
                    "thread_status": "running",
                    "task_result": "",
                }
            ]
            assert (
                client.get(
                    f"/api/agent-threads/{grandchild.node.thread_id}/children",
                    params={"session_id": sidebar["session_id"]},
                ).json()
                == []
            )

            response = client.post(
                f"/api/agent-threads/{child.node.thread_id}/messages",
                json={
                    "session_id": sidebar["session_id"],
                    "content": "inspect the file",
                    "permission_mode": "workspace_write",
                    "running_mode": "plan",
                },
            )
            assert response.status_code == 202, response.text
            assert response.json()["target_state"] == "running"
            envelope = queue.peek_thread(child.node.thread_id)
            assert envelope is not None
            assert envelope.source_thread_id == sidebar["session_id"]
            assert envelope.references == ()
            assert envelope.payload["runtime_config"]["permission_mode"] == "workspace_write"

            fork_message = client.post(
                f"/api/agent-threads/{fork_child.node.thread_id}/messages",
                json={
                    "session_id": sidebar["session_id"],
                    "content": "fork-only message",
                },
            )
            assert fork_message.status_code == 202, fork_message.text
            fork_envelope = queue.peek_thread(fork_child.node.thread_id)
            assert fork_envelope is not None
            assert fork_envelope.source_thread_id == fork_thread_id

            source_runner = AgentRunner(_AnswerPlanner(), ToolRegistry())
            source_runtime = source_runner.new_runtime(task="root", session_id=sidebar["session_id"])
            source_runtime.run.thread_id = sidebar["thread_id"]
            source_runtime.run.turn_id = source.id
            source_runtime.services.runtime_node_context = lambda: [source]
            with pytest.raises(ToolError, match="does not match"):
                state.subagent_coordinator.invoke(
                    source_runtime,
                    "send_agent_message",
                    {
                        "source_thread_id": fork_thread_id,
                        "target_thread_path": "/root/worker",
                        "subagent_task": "spoofed cross-tree message",
                    },
                )
            source_runner.close()

            nested_message = client.post(
                f"/api/agent-threads/{grandchild.node.thread_id}/messages",
                json={
                    "session_id": sidebar["session_id"],
                    "content": "ask the nested worker",
                },
            )
            assert nested_message.status_code == 202
            nested_envelope = queue.peek_thread(grandchild.node.thread_id)
            assert nested_envelope is not None
            assert nested_envelope.source_thread_id == sidebar["session_id"]

            unknown = client.get(
                "/api/agent-threads/thread_missing/children",
                params={"session_id": sidebar["session_id"]},
            )
            assert unknown.status_code == 404

            other = client.post("/api/sidebar-threads", json={}).json()
            crossed = client.get(
                f"/api/agent-threads/{child.node.thread_id}/children",
                params={"session_id": other["session_id"]},
            )
            assert crossed.status_code == 404
            crossed_stream = client.get(
                f"/api/agent-threads/{child.node.thread_id}/stream",
                params={"session_id": other["session_id"]},
            )
            assert crossed_stream.status_code == 404
    finally:
        state.close()


def test_fork_sidebar_root_can_delegate_its_own_subagent(tmp_path: Path) -> None:
    index = AgentThreadIndex()
    store = SQLiteSessionStore(ClientPaths(tmp_path / "fork-agents"), index)
    queue = MemoryMessageQueue()
    registry = JobRegistry()
    session = store.create_session("fork agents")
    store.create_sidebar_thread(
        session_id=session.session_id,
        thread_id=session.session_id,
        title="fork agents",
    )
    source = _finished_source(store, session.session_id)
    fork = store.fork_turn_node(source.id, new_turn_id="turn_fork_agent", thread_id="thread_fork_agent")
    store.create_sidebar_thread(
        session_id=session.session_id,
        thread_id=fork.thread_id,
        title="fork agents（分支）",
    )

    parent_runner = AgentRunner(_AnswerPlanner(), ToolRegistry())
    runtime = parent_runner.new_runtime(task="fork root", session_id=session.session_id)
    runtime.run.thread_id = fork.thread_id
    runtime.run.turn_id = fork.id
    runtime.services.runtime_node_context = lambda: [fork]
    coordinator = SubagentCoordinator(
        store=store,
        message_queue=queue,
        index=index,
        job_registry=registry,
    )
    coordinator.bind_session(
        session.session_id,
        lambda: AgentRunner(_AnswerPlanner(), ToolRegistry(), job_registry=registry),
        tmp_path,
    )
    try:
        delegated = json.loads(
            coordinator.invoke(
                runtime,
                "delegate_tasks",
                {
                    "subagent_path": "/root/worker",
                    "subagent_task": "fork-only task",
                    "context_transfer_strategy": "independent",
                },
            )
        )
        assert delegated == {"thread_path": "/root/worker", "thread_status": "running"}
        child_id = index.thread_for_path(session.session_id, fork.thread_id, "/root/worker")
        assert child_id is not None
        child = store.get_thread_node(session.session_id, child_id)
        assert child is not None and child.root_thread_id == fork.thread_id
        assert child.parent_thread_id == fork.thread_id and child.thread_path == "/root/worker"
        assert store.list_child_thread_nodes(session.session_id, session.session_id) == []

        deadline = monotonic() + 5
        while monotonic() < deadline:
            child_runtime = store.get_runtime_thread(session.session_id, child_id)
            if child_runtime is not None and child_runtime.running_turn_id is None:
                break
            sleep(0.01)
        else:
            pytest.fail("fork-owned Subagent did not finish")
    finally:
        registry.close_all(reason="test complete", timeout=5)
        parent_runner.close()


def test_delegate_terminal_result_is_delivered_once_as_plain_text_assistant_report(tmp_path: Path) -> None:
    index = AgentThreadIndex()
    store = SQLiteSessionStore(ClientPaths(tmp_path / "data"), index)
    queue = MemoryMessageQueue()
    registry = JobRegistry()
    session = store.create_session("assistant report")
    source = _finished_source(store, session.session_id)
    parent_runner = AgentRunner(_AnswerPlanner(), ToolRegistry())
    runtime = parent_runner.new_runtime(task="parent", session_id=session.session_id)
    runtime.run.thread_id = session.session_id
    runtime.run.turn_id = source.id
    runtime.services.runtime_node_context = lambda: [source]
    coordinator = SubagentCoordinator(
        settings=SubagentSettings(max_workers=2),
        store=store,
        message_queue=queue,
        index=index,
        job_registry=registry,
    )
    coordinator.bind_session(
        session.session_id,
        lambda: AgentRunner(_AnswerPlanner(), ToolRegistry(), job_registry=registry),
        tmp_path,
    )
    try:
        coordinator.invoke(
            runtime,
            "delegate_tasks",
            {
                "subagent_path": "/root/reporter",
                "subagent_task": "中文结果",
                "context_transfer_strategy": "independent",
            },
        )
        deadline = monotonic() + 5
        while monotonic() < deadline:
            reports = store.list_agent_turn_reports(session.session_id, states=("delivered",))
            if len(reports) == 1:
                break
            sleep(0.01)
        else:
            pytest.fail("Agent report was not delivered")

        expected = "thread_path: /root/reporter\nthread_status: success\ntask_result: done:中文结果"
        assert reports[0].reply_content == expected
        assert reports[0].recipient_thread_id == session.session_id
        assert reports[0].delivery_id.startswith("agent_report_")

        received = store.get_node(session.session_id, source.id)
        assert isinstance(received, RuntimeState)
        assert [message["role"] for message in received.data[received.current_data_idx]] == [
            "user",
            "assistant",
            "assistant",
        ]
        assert received.data[received.current_data_idx][-1] == {
            "role": "assistant",
            "content": [
                {
                    "type": "subagent",
                    "event": "agent_report",
                    "status": "success",
                    "text": expected,
                    "delivery_id": reports[0].delivery_id,
                }
            ],
        }
        assert not any(
            item.get("event") == "subagent_initial_result"
            for message in received.data[received.current_data_idx]
            for item in message.get("content", [])
        )

        coordinator._drain_inactive_reports(session.session_id, session.session_id)
        replayed = store.get_node(session.session_id, source.id)
        assert isinstance(replayed, RuntimeState)
        assert len(replayed.data[replayed.current_data_idx]) == 3
    finally:
        coordinator.close()
        registry.close_all(reason="test cleanup", timeout=2)
        parent_runner.close()


def test_one_agent_turn_reports_once_to_every_registered_sender(tmp_path: Path) -> None:
    index = AgentThreadIndex()
    store = SQLiteSessionStore(ClientPaths(tmp_path / "data"), index)
    queue = MemoryMessageQueue()
    registry = JobRegistry()
    session = store.create_session("multiple report recipients")
    root = _finished_source(store, session.session_id)
    sender = _agent_create(session.session_id, root, name="sender")
    worker = _agent_create(session.session_id, root, name="worker")
    store.create_agent_thread(session.session_id, sender)
    store.create_agent_thread(session.session_id, worker)

    sender_turn = sender.turn.clone()
    sender_turn.data[0][1]["content"].append({"type": "text", "text": "sender ready", "status": "success"})
    sender_turn.status = "success"
    store.finalize_node(RuntimeState.from_dict(sender_turn.to_dict()))
    store.register_agent_turn_report(
        session.session_id,
        worker.turn.id,
        worker.node.thread_id,
        session.session_id,
    )
    store.register_agent_turn_report(
        session.session_id,
        worker.turn.id,
        worker.node.thread_id,
        sender.node.thread_id,
    )

    completed = worker.turn.clone()
    completed.data[0][1]["content"].append({"type": "text", "text": "shared result", "status": "success"})
    completed.status = "success"
    completed = RuntimeState.from_dict(completed.to_dict())
    store.finalize_node(completed)
    coordinator = SubagentCoordinator(store=store, message_queue=queue, index=index, job_registry=registry)
    try:
        coordinator._publish_turn_reports(worker.node, completed)
        reports = store.list_agent_turn_reports(session.session_id, states=("delivered",))
        assert len(reports) == 2
        assert {report.recipient_thread_id for report in reports} == {
            session.session_id,
            sender.node.thread_id,
        }
        assert len({report.delivery_id for report in reports}) == 2
        assert len({report.reply_content for report in reports}) == 1

        for recipient_thread_id, turn_id in (
            (session.session_id, root.id),
            (sender.node.thread_id, sender.turn.id),
        ):
            received = store.get_node(session.session_id, turn_id)
            assert isinstance(received, RuntimeState)
            report_items = [
                item
                for message in received.data[received.current_data_idx]
                for item in message.get("content", [])
                if item.get("event") == "agent_report"
            ]
            assert len(report_items) == 1
            delivery_id = report_items[0]["delivery_id"]
            assert store.agent_report_statuses(session.session_id, {delivery_id}) == {delivery_id: "success"}
            coordinator._drain_inactive_reports(session.session_id, recipient_thread_id)
            replayed = store.get_node(session.session_id, turn_id)
            assert isinstance(replayed, RuntimeState)
            assert (
                sum(
                    item.get("event") == "agent_report"
                    for message in replayed.data[replayed.current_data_idx]
                    for item in message.get("content", [])
                )
                == 1
            )
    finally:
        coordinator.close()
        registry.close_all(reason="test cleanup", timeout=2)


def test_report_replay_after_sqlite_delivery_before_queue_ack_is_idempotent(tmp_path: Path) -> None:
    class CrashBeforeAckQueue(MemoryMessageQueue):
        fail_ack = True

        def ack(self, claimed):
            if self.fail_ack and claimed.envelope.target_kind == "report":
                self.fail_ack = False
                raise RuntimeError("simulated crash before Redis ACK")
            return super().ack(claimed)

    index = AgentThreadIndex()
    store = SQLiteSessionStore(ClientPaths(tmp_path / "data"), index)
    queue = CrashBeforeAckQueue()
    registry = JobRegistry()
    session = store.create_session("report crash replay")
    root = _finished_source(store, session.session_id)
    worker = _agent_create(session.session_id, root, name="worker")
    store.create_agent_thread(session.session_id, worker)
    store.register_agent_turn_report(
        session.session_id,
        worker.turn.id,
        worker.node.thread_id,
        session.session_id,
    )
    completed = worker.turn.clone()
    completed.data[0][1]["content"].append({"type": "text", "text": "durable", "status": "success"})
    completed.status = "success"
    completed = RuntimeState.from_dict(completed.to_dict())
    store.finalize_node(completed)
    coordinator = SubagentCoordinator(store=store, message_queue=queue, index=index, job_registry=registry)
    try:
        content = coordinator._reply_content(worker.node, completed)
        coordinator._reply_context.value = (session.session_id, completed.id, "success")
        try:
            coordinator.reply_subagent_message(content)
        finally:
            del coordinator._reply_context.value
        coordinator._dispatch_ready_reports(session.session_id)

        delivered = store.list_agent_turn_reports(session.session_id, states=("delivered",))
        assert len(delivered) == 1
        after_crash = store.get_node(session.session_id, root.id)
        assert isinstance(after_crash, RuntimeState)
        assert len(after_crash.data[after_crash.current_data_idx]) == 3

        recovered = SubagentCoordinator(store=store, message_queue=queue, index=index, job_registry=registry)
        try:
            recovered._drain_inactive_reports(session.session_id, session.session_id)
        finally:
            recovered.close()
        replayed = store.get_node(session.session_id, root.id)
        assert isinstance(replayed, RuntimeState)
        assert len(replayed.data[replayed.current_data_idx]) == 3
        assert queue.pending_deliveries() == []
    finally:
        coordinator.close()
        registry.close_all(reason="test cleanup", timeout=2)


def test_waiting_report_survives_queue_outage_and_dispatches_after_restart(tmp_path: Path) -> None:
    class UnavailableReportQueue(MemoryMessageQueue):
        def dispatch_report(self, envelope):
            raise MessageQueueUnavailable("message_queue_unavailable")

    index = AgentThreadIndex()
    store = SQLiteSessionStore(ClientPaths(tmp_path / "data"), index)
    registry = JobRegistry()
    session = store.create_session("waiting report restart")
    root = _finished_source(store, session.session_id)
    worker = _agent_create(session.session_id, root, name="worker")
    store.create_agent_thread(session.session_id, worker)
    store.register_agent_turn_report(
        session.session_id,
        worker.turn.id,
        worker.node.thread_id,
        session.session_id,
    )
    completed = worker.turn.clone()
    completed.data[0][1]["content"].append({"type": "text", "text": "after outage", "status": "success"})
    completed.status = "success"
    completed = RuntimeState.from_dict(completed.to_dict())
    store.finalize_node(completed)

    unavailable = SubagentCoordinator(
        store=store,
        message_queue=UnavailableReportQueue(),
        index=index,
        job_registry=registry,
    )
    content = unavailable._reply_content(worker.node, completed)
    unavailable._reply_context.value = (session.session_id, completed.id, "success")
    try:
        unavailable.reply_subagent_message(content)
    finally:
        del unavailable._reply_context.value
    unavailable._dispatch_ready_reports(session.session_id)
    unavailable.close()
    assert len(store.list_agent_turn_reports(session.session_id, states=("waiting",))) == 1

    queue = MemoryMessageQueue()
    recovered = SubagentCoordinator(store=store, message_queue=queue, index=index, job_registry=registry)
    try:
        recovered._dispatch_ready_reports(session.session_id)
        assert len(store.list_agent_turn_reports(session.session_id, states=("delivered",))) == 1
        received = store.get_node(session.session_id, root.id)
        assert isinstance(received, RuntimeState)
        assert received.data[received.current_data_idx][-1]["content"][0]["text"] == content
    finally:
        recovered.close()
        registry.close_all(reason="test cleanup", timeout=2)


def test_failed_agent_report_uses_exact_single_newline_retry_text(tmp_path: Path) -> None:
    index = AgentThreadIndex()
    store = SQLiteSessionStore(ClientPaths(tmp_path / "data"), index)
    queue = MemoryMessageQueue()
    registry = JobRegistry()
    session = store.create_session("failed assistant report")
    source = _finished_source(store, session.session_id)

    class FailingPlanner:
        name = "failing"

        def decide(self, _runtime):
            raise RuntimeError("精确错误")

    parent_runner = AgentRunner(_AnswerPlanner(), ToolRegistry())
    runtime = parent_runner.new_runtime(task="parent", session_id=session.session_id)
    runtime.run.thread_id = session.session_id
    runtime.run.turn_id = source.id
    runtime.services.runtime_node_context = lambda: [source]
    coordinator = SubagentCoordinator(
        store=store,
        message_queue=queue,
        index=index,
        job_registry=registry,
    )
    coordinator.bind_session(
        session.session_id,
        lambda: AgentRunner(FailingPlanner(), ToolRegistry(), job_registry=registry),
        tmp_path,
    )
    try:
        coordinator.invoke(
            runtime,
            "delegate_tasks",
            {
                "subagent_path": "/root/failure",
                "subagent_task": "fail",
                "context_transfer_strategy": "independent",
            },
        )
        deadline = monotonic() + 5
        while monotonic() < deadline:
            reports = store.list_agent_turn_reports(session.session_id, states=("delivered",))
            if reports:
                break
            sleep(0.01)
        else:
            pytest.fail("Failed Agent report was not delivered")
        assert reports[0].reply_content == (
            "thread_path: /root/failure\n"
            "thread_status: failed\n"
            "task_result: 精确错误\n"
            "This agent's turn failed. If you still need this agent, "
            "use the send_agent_message tool to give it another task."
        )
        assert "精确错误\n\nThis agent" not in reports[0].reply_content
    finally:
        coordinator.close()
        registry.close_all(reason="test cleanup", timeout=2)
        parent_runner.close()


def test_pause_current_turn_waits_for_child_report_and_resumes_the_same_turn(tmp_path: Path) -> None:
    index = AgentThreadIndex()
    store = SQLiteSessionStore(ClientPaths(tmp_path / "data"), index)
    queue = MemoryMessageQueue()
    registry = JobRegistry()
    session = store.create_session("pause for child")
    source = _finished_source(store, session.session_id)

    class WaitingPlanner:
        name = "waiting"

        def decide(self, child_runtime):
            if child_runtime.run.task == "child work":
                sleep(0.2)
                return AssistantMessage(content="child completed")
            messages = child_runtime.model_messages()
            reports = [
                message.content
                for message in messages
                if isinstance(message, AssistantMessage) and message.name == "subagent_report"
            ]
            if reports:
                assert reports == [
                    "thread_path: /root/waiter/worker\nthread_status: success\ntask_result: child completed"
                ]
                return AssistantMessage(content="waiter resumed")
            delegated = any(
                tool.name == "delegate_tasks" and tool.status == "succeeded"
                for message in messages
                if isinstance(message, AssistantMessage)
                for tool in message.tool_messages
            )
            if not delegated:
                return AssistantMessage(
                    tool_messages=[
                        ToolMessage(
                            name="delegate_tasks",
                            call_id="delegate_waited_child",
                            arguments={
                                "subagent_path": "/root/waiter/worker",
                                "subagent_task": "child work",
                                "context_transfer_strategy": "independent",
                            },
                        )
                    ]
                )
            return AssistantMessage(
                tool_messages=[
                    ToolMessage(name="pause_current_turn", call_id="pause_waiter", arguments={}),
                    ToolMessage(name="get_thread_node", call_id="must_not_run", arguments={}),
                ]
            )

    parent_runner = AgentRunner(_AnswerPlanner(), ToolRegistry())
    runtime = parent_runner.new_runtime(task="parent", session_id=session.session_id)
    runtime.run.thread_id = session.session_id
    runtime.run.turn_id = source.id
    runtime.services.runtime_node_context = lambda: [source]
    coordinator = SubagentCoordinator(
        settings=SubagentSettings(max_workers=3, max_depth=2),
        store=store,
        message_queue=queue,
        index=index,
        job_registry=registry,
    )
    coordinator.bind_session(
        session.session_id,
        lambda: AgentRunner(
            WaitingPlanner(),
            ToolRegistry(list(delegation_tools())),
            job_registry=registry,
        ),
        tmp_path,
    )
    try:
        coordinator.invoke(
            runtime,
            "delegate_tasks",
            {
                "subagent_path": "/root/waiter",
                "subagent_task": "wait",
                "context_transfer_strategy": "independent",
            },
        )
        waiter_id = index.thread_for_path(session.session_id, session.session_id, "/root/waiter")
        assert waiter_id is not None
        initial_turn_id = index.head_for_thread(waiter_id)
        deadline = monotonic() + 8
        observed_paused = False
        while monotonic() < deadline:
            waiter = store.get_thread_node(session.session_id, waiter_id)
            if waiter is not None and waiter.thread_status == "paused":
                observed_paused = True
            if waiter is not None and waiter.thread_status == "success":
                break
            sleep(0.01)
        else:
            pytest.fail("The paused waiter did not resume after its child report")

        assert observed_paused is True
        assert index.head_for_thread(waiter_id) == initial_turn_id
        waiter_turn = store.get_node(session.session_id, initial_turn_id)
        assert isinstance(waiter_turn, RuntimeState)
        assert waiter_turn.status == "success"
        report_items = [
            item
            for message in waiter_turn.data[waiter_turn.current_data_idx]
            for item in message.get("content", [])
            if item.get("type") == "subagent" and item.get("event") == "agent_report"
        ]
        assert len(report_items) == 1
        pause_results = [
            item
            for message in waiter_turn.data[waiter_turn.current_data_idx]
            for item in message.get("content", [])
            if item.get("type") == "tool_result" and item.get("call_id") == "pause_waiter"
        ]
        assert pause_results[0]["content"] == "thread_status: paused"
        skipped = [
            item
            for message in waiter_turn.data[waiter_turn.current_data_idx]
            for item in message.get("content", [])
            if item.get("type") == "tool_result" and item.get("call_id") == "must_not_run"
        ]
        assert skipped == []
    finally:
        coordinator.close()
        registry.close_all(reason="test cleanup", timeout=5)
        parent_runner.close()


def test_send_agent_message_registers_reports_only_when_need_reply_is_true(tmp_path: Path) -> None:
    index = AgentThreadIndex()
    store = SQLiteSessionStore(ClientPaths(tmp_path / "data"), index)
    queue = MemoryMessageQueue()
    registry = JobRegistry()
    session = store.create_session("optional send reply")
    source = _finished_source(store, session.session_id)
    parent_runner = AgentRunner(_AnswerPlanner(), ToolRegistry())
    runtime = parent_runner.new_runtime(task="parent", session_id=session.session_id)
    runtime.run.thread_id = session.session_id
    runtime.run.turn_id = source.id
    runtime.services.runtime_node_context = lambda: [source]
    coordinator = SubagentCoordinator(
        store=store,
        message_queue=queue,
        index=index,
        job_registry=registry,
    )
    coordinator.bind_session(
        session.session_id,
        lambda: AgentRunner(_AnswerPlanner(), ToolRegistry(), job_registry=registry),
        tmp_path,
    )

    def wait_for_worker(expected_status: str = "success") -> str:
        worker_id = index.thread_for_path(session.session_id, session.session_id, "/root/worker")
        assert worker_id is not None
        deadline = monotonic() + 5
        while monotonic() < deadline:
            worker = store.get_thread_node(session.session_id, worker_id)
            if worker is not None and worker.thread_status == expected_status:
                return worker_id
            sleep(0.01)
        pytest.fail("worker did not settle")

    try:
        coordinator.invoke(
            runtime,
            "delegate_tasks",
            {
                "subagent_path": "/root/worker",
                "subagent_task": "initial",
                "context_transfer_strategy": "independent",
            },
        )
        worker_id = wait_for_worker()
        deadline = monotonic() + 5
        while monotonic() < deadline:
            if len(store.list_agent_turn_reports(session.session_id, states=("delivered",))) == 1:
                break
            sleep(0.01)
        else:
            pytest.fail("initial delegate report did not settle")

        coordinator.invoke(
            runtime,
            "send_agent_message",
            {"target_thread_path": "/root/worker", "subagent_task": "no reply"},
        )
        wait_for_worker()
        no_reply_head = index.head_for_thread(worker_id)
        reports = store.list_agent_turn_reports(session.session_id)
        assert all(report.turn_id != no_reply_head for report in reports)

        coordinator.invoke(
            runtime,
            "send_agent_message",
            {
                "target_thread_path": "/root/worker",
                "subagent_task": "reply please",
                "need_reply": True,
            },
        )
        wait_for_worker()
        reply_head = index.head_for_thread(worker_id)
        deadline = monotonic() + 5
        while monotonic() < deadline:
            reports = store.list_agent_turn_reports(session.session_id, states=("delivered",))
            if any(report.turn_id == reply_head for report in reports):
                break
            sleep(0.01)
        else:
            pytest.fail("need_reply Agent message did not report")
        report = next(report for report in reports if report.turn_id == reply_head)
        assert report.recipient_thread_id == session.session_id
        assert report.reply_content.endswith("task_result: done:reply please")
    finally:
        coordinator.close()
        registry.close_all(reason="test cleanup", timeout=5)
        parent_runner.close()


def test_running_agent_inserts_report_at_safe_boundary_and_continues(tmp_path: Path) -> None:
    index = AgentThreadIndex()
    store = SQLiteSessionStore(ClientPaths(tmp_path / "data"), index)
    queue = MemoryMessageQueue()
    registry = JobRegistry()
    session = store.create_session("running report")
    source = _finished_source(store, session.session_id)
    release_quick_child = Event()

    class RunningReportPlanner:
        name = "running-report"

        def decide(self, child_runtime):
            if child_runtime.run.task == "quick child":
                if not release_quick_child.wait(timeout=5):
                    raise RuntimeError("parent Agent did not reach the report boundary")
                return AssistantMessage(content="quick result")
            messages = child_runtime.model_messages()
            if any(isinstance(message, AssistantMessage) and message.name == "subagent_report" for message in messages):
                return AssistantMessage(content="continued after report")
            delegated = any(
                tool.name == "delegate_tasks" and tool.status == "succeeded"
                for message in messages
                if isinstance(message, AssistantMessage)
                for tool in message.tool_messages
            )
            if delegated:
                release_quick_child.set()
                deadline = monotonic() + 5
                while monotonic() < deadline:
                    queued_reports = store.list_agent_turn_reports(
                        session.session_id,
                        states=("queued",),
                    )
                    if any(
                        report.recipient_thread_id == child_runtime.run.thread_id and report.thread_status == "success"
                        for report in queued_reports
                    ):
                        break
                    sleep(0.01)
                else:
                    raise RuntimeError("completed child report was not queued at the tool boundary")
                return AssistantMessage(
                    tool_messages=[ToolMessage(name="get_thread_node", call_id="boundary_tool", arguments={})]
                )
            return AssistantMessage(
                tool_messages=[
                    ToolMessage(
                        name="delegate_tasks",
                        call_id="delegate_quick_child",
                        arguments={
                            "subagent_path": "/root/running/worker",
                            "subagent_task": "quick child",
                            "context_transfer_strategy": "independent",
                        },
                    )
                ]
            )

    parent_runner = AgentRunner(_AnswerPlanner(), ToolRegistry())
    runtime = parent_runner.new_runtime(task="parent", session_id=session.session_id)
    runtime.run.thread_id = session.session_id
    runtime.run.turn_id = source.id
    runtime.services.runtime_node_context = lambda: [source]
    coordinator = SubagentCoordinator(
        settings=SubagentSettings(max_workers=3, max_depth=2),
        store=store,
        message_queue=queue,
        index=index,
        job_registry=registry,
    )
    coordinator.bind_session(
        session.session_id,
        lambda: AgentRunner(
            RunningReportPlanner(),
            ToolRegistry(list(delegation_tools())),
            job_registry=registry,
        ),
        tmp_path,
    )
    try:
        coordinator.invoke(
            runtime,
            "delegate_tasks",
            {
                "subagent_path": "/root/running",
                "subagent_task": "run",
                "context_transfer_strategy": "independent",
            },
        )
        running_id = index.thread_for_path(session.session_id, session.session_id, "/root/running")
        assert running_id is not None
        turn_id = index.head_for_thread(running_id)
        deadline = monotonic() + 8
        while monotonic() < deadline:
            node = store.get_thread_node(session.session_id, running_id)
            if node is not None and node.thread_status == "success":
                break
            sleep(0.01)
        else:
            pytest.fail("running report recipient did not continue")

        assert index.head_for_thread(running_id) == turn_id
        completed = store.get_node(session.session_id, turn_id)
        assert isinstance(completed, RuntimeState)
        selected = completed.data[completed.current_data_idx]
        report_indexes = [
            index
            for index, message in enumerate(selected)
            if message.get("role") == "assistant" and message.get("content", [{}])[0].get("event") == "agent_report"
        ]
        assert len(report_indexes) == 1
        report_index = report_indexes[0]
        assert len(selected[report_index]["content"]) == 1
        assert selected[report_index + 1]["content"][-1]["text"] == "continued after report"
        boundary_results = [
            item
            for message in selected
            for item in message.get("content", [])
            if item.get("type") == "tool_result" and item.get("call_id") == "boundary_tool"
        ]
        assert len(boundary_results) == 1
        assert boundary_results[0]["status"] == "failed"
        assert boundary_results[0]["content"] == "Not executed because a subagent report arrived."
    finally:
        coordinator.close()
        registry.close_all(reason="test cleanup", timeout=5)
        parent_runner.close()


def test_agent_thread_message_returns_503_when_redis_is_unavailable(tmp_path: Path) -> None:
    class UnavailableQueue(MemoryMessageQueue):
        def dispatch_agent(self, envelope: MessageEnvelope) -> MessageEnvelope:
            del envelope
            raise MessageQueueUnavailable("redis unavailable")

    state = WebAppState(tmp_path / "web-agent-redis-error", message_queue=UnavailableQueue())
    try:
        with TestClient(create_app(state)) as client:
            sidebar = client.post("/api/sidebar-threads", json={}).json()
            store = SQLiteSessionStore(state.paths, state.agent_thread_index)
            source = _finished_source(store, sidebar["session_id"])
            child = _agent_create(sidebar["session_id"], source, name="worker")
            store.create_agent_thread(sidebar["session_id"], child)

            response = client.post(
                f"/api/agent-threads/{child.node.thread_id}/messages",
                json={"session_id": sidebar["session_id"], "content": "must fail closed"},
            )
            assert response.status_code == 503
            assert response.json() == {"detail": "redis unavailable"}
    finally:
        state.close()


def test_agent_thread_failed_admission_publishes_terminal_without_closing_channel(tmp_path: Path) -> None:
    queue = MemoryMessageQueue()
    state = WebAppState(tmp_path / "web-agent-admission", message_queue=queue)
    try:
        with TestClient(create_app(state)) as client:
            sidebar = client.post("/api/sidebar-threads", json={}).json()
            store = SQLiteSessionStore(state.paths, state.agent_thread_index)
            source = _finished_source(store, sidebar["session_id"])
            child = _agent_create(sidebar["session_id"], source, name="worker")
            store.create_agent_thread(sidebar["session_id"], child)
            initial = child.turn.clone()
            initial.status = "success"
            store.finalize_node(initial)
            subscription = state.agent_thread_events.subscribe(sidebar["session_id"], child.node.thread_id)
            assert subscription.next_event()["type"] == "thread.ready"

            response = client.post(
                f"/api/agent-threads/{child.node.thread_id}/messages",
                json={"session_id": sidebar["session_id"], "content": "wake without a runner"},
            )
            assert response.status_code == 202
            assert response.json()["background_admission"] == "rejected:no_runner"
            assert subscription.next_event()["type"] == "turn.snapshot"
            failed = subscription.next_event()
            assert failed["type"] == "turn.snapshot"
            assert failed["turn"]["status"] == "failed"
            terminal = subscription.next_event()
            assert terminal["type"] == "turn.terminal"
            assert terminal["status"] == "failed"
            assert subscription.closed is False
    finally:
        state.close()


def test_idle_turn_creation_uses_sqlite_cas(tmp_path: Path) -> None:
    store = SQLiteSessionStore(ClientPaths(tmp_path / "data"))
    session = store.create_session("cas")
    source = _finished_source(store, session.session_id)

    def create(suffix: str) -> str:
        node = RuntimeState.create(
            session_id=session.session_id,
            thread_id=session.session_id,
            id=f"turn_{suffix}",
            parent=source,
            user_content=suffix,
        )
        return store.create_thread_turn_if_idle(node, expected_head_id=source.id).id

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(create, suffix) for suffix in ("one", "two")]
    outcomes = []
    for future in futures:
        try:
            outcomes.append(future.result())
        except ValueError:
            outcomes.append("conflict")
    assert outcomes.count("conflict") == 1
    runtime_thread = store.get_runtime_thread(session.session_id, session.session_id)
    assert runtime_thread is not None and runtime_thread.running_turn_id in {"turn_one", "turn_two"}


def _create_legacy_database(path: Path, session_id: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    timestamp = utc_iso()
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE store_metadata(
                session_id TEXT PRIMARY KEY,
                schema_version INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE thread_nodes(
                session_id TEXT NOT NULL,
                thread_id TEXT PRIMARY KEY,
                thread_task TEXT NOT NULL,
                thread_status TEXT NOT NULL
            );
            """
        )
        connection.execute(
            "INSERT INTO store_metadata VALUES (?,13,?,?)",
            (session_id, timestamp, timestamp),
        )


def test_legacy_schema_is_rejected_without_mutating_or_deleting_database(tmp_path: Path) -> None:
    paths = ClientPaths(tmp_path / "data")
    paths.ensure()
    session_id = "session_legacy"
    database = paths.session_db(session_id)
    _create_legacy_database(database, session_id)
    before = database.read_bytes()

    with pytest.raises(RuntimeError, match="requires v16 and left the database untouched"):
        SQLiteSessionStore(paths).get_session(session_id)
    assert database.exists()
    assert database.read_bytes() == before


def test_persistent_delegate_reports_result_and_accepts_follow_up(tmp_path: Path) -> None:
    session_workspace = tmp_path / "session-workspace"
    project_workspace = tmp_path / "project-workspace"
    session_workspace.mkdir()
    project_workspace.mkdir()
    paths = ClientPaths(tmp_path / "data")
    index = AgentThreadIndex()
    store = SQLiteSessionStore(paths, index)
    queue = MemoryMessageQueue()
    registry = JobRegistry()
    session = store.create_session("agents")
    root = store.ensure_root_node(session.session_id, id="root")
    source = NodeWriter(store).create(
        RuntimeState.create(
            session_id=session.session_id,
            thread_id=session.session_id,
            id="source",
            parent=root,
            user_content="parent",
            cwd=str(session_workspace),
            project_cwd=str(project_workspace),
        )
    )
    parent_runner = AgentRunner(
        _AnswerPlanner(),
        ToolRegistry(),
        workspace_root=str(session_workspace),
        project_cwd=str(project_workspace),
    )
    runtime = parent_runner.new_runtime(task="parent", session_id=session.session_id)
    runtime.run.thread_id = session.session_id
    runtime.run.turn_id = source.id
    runtime.services.runtime_node_context = lambda: [source]

    follow_up_started = Event()
    release_follow_up = Event()

    class ChildPlanner(_AnswerPlanner):
        def decide(self, child_runtime):
            if child_runtime.run.task == "follow-up":
                follow_up_started.set()
                assert release_follow_up.wait(5)
            return super().decide(child_runtime)

    coordinator = SubagentCoordinator(
        settings=SubagentSettings(max_workers=2),
        store=store,
        message_queue=queue,
        index=index,
        job_registry=registry,
    )

    def child_factory() -> AgentRunner:
        return AgentRunner(
            ChildPlanner(),
            ToolRegistry(list(delegation_tools())),
            job_registry=registry,
        )

    coordinator.bind_session(session.session_id, child_factory, session_workspace, project_workspace)
    delegated = json.loads(
        coordinator.invoke(
            runtime,
            "delegate_tasks",
            {
                "subagent_path": "/root/worker",
                "subagent_task": "first",
                "context_transfer_strategy": "independent",
            },
        )
    )
    assert delegated == {"thread_path": "/root/worker", "thread_status": "running"}

    deadline = monotonic() + 5
    while monotonic() < deadline:
        children = store.list_child_thread_nodes(session.session_id, session.session_id)
        if len(children) == 1 and children[0].thread_status == "success":
            break
        sleep(0.01)
    else:
        pytest.fail("background Agent Turn did not finish")

    children = store.list_child_thread_nodes(session.session_id, session.session_id)
    child = children[0]
    first_turn_id = index.head_for_thread(child.thread_id)
    first_turn = store.get_node(session.session_id, first_turn_id or "")
    assert isinstance(first_turn, RuntimeState)
    assert first_turn.cwd == str(session_workspace.resolve())
    assert first_turn.project_cwd == str(project_workspace.resolve())
    deadline = monotonic() + 5
    while monotonic() < deadline:
        reports = store.list_agent_turn_reports(session.session_id, states=("queued",))
        if len(reports) == 1:
            break
        sleep(0.01)
    else:
        pytest.fail("automatic Agent report was not queued")
    assert reports[0].recipient_thread_id == session.session_id
    assert coordinator.consume_runtime_reports(runtime) == 1
    assert queue.pending_deliveries() == []
    delivered_reports = store.list_agent_turn_reports(session.session_id, states=("delivered",))
    assert [report.delivery_id for report in delivered_reports] == [reports[0].delivery_id]
    assert json.loads(coordinator.invoke(runtime, "get_thread_node", {})) == [
        {
            "thread_path": "/root/worker",
            "thread_status": "success",
            "task_result": "done:first",
        }
    ]

    with pytest.raises(ToolError, match="does not match"):
        coordinator.invoke(
            runtime,
            "send_agent_message",
            {
                "source_thread_id": "another-thread",
                "target_thread_path": "/root/worker",
                "subagent_task": "rejected for the wrong source",
            },
        )
    follow_up = json.loads(
        coordinator.invoke(
            runtime,
            "send_agent_message",
            {
                "target_thread_path": "/root/worker",
                "subagent_task": "follow-up",
            },
        )
    )
    assert follow_up == {"thread_path": "/root/worker", "thread_status": "running"}
    assert follow_up_started.wait(5)
    current = store.get_runtime_thread(session.session_id, child.thread_id)
    assert current is not None and current.current_turn_id != first_turn_id
    follow_up_turn_id = current.current_turn_id
    release_follow_up.set()
    deadline = monotonic() + 5
    while monotonic() < deadline:
        current = store.get_runtime_thread(session.session_id, child.thread_id)
        if current is not None and current.running_turn_id is None and current.current_turn_id == follow_up_turn_id:
            break
        sleep(0.01)
    else:
        pytest.fail("idle Agent mailbox delivery did not finish")
    delivered = store.get_node(session.session_id, follow_up_turn_id or "")
    assert isinstance(delivered, RuntimeState) and delivered.status == "success"
    assert delivered.user_message["content"][0]["text"] == "follow-up"
    assert delivered.user_message.get("delivery_id")
    coordinator.close()
    registry.close_all(reason="test complete", timeout=5)
    parent_runner.close()


def test_running_subagent_bridge_accepts_live_runtime_config(tmp_path: Path) -> None:
    index = AgentThreadIndex()
    store = SQLiteSessionStore(ClientPaths(tmp_path / "data"), index)
    queue = MemoryMessageQueue()
    registry = JobRegistry()
    session = store.create_session("live config")
    source = _finished_source(store, session.session_id)
    started = Event()
    release = Event()
    observed_pending: dict[str, object] = {}

    class BlockingPlanner:
        name = "blocking"

        def decide(self, runtime):
            started.set()
            assert release.wait(5), "test did not release the blocking Subagent"
            observed_pending.update(runtime.services.pending_runtime_config or {})
            return AssistantMessage(content="configured")

    parent_runner = AgentRunner(_AnswerPlanner(), ToolRegistry())
    runtime = parent_runner.new_runtime(task="parent", session_id=session.session_id)
    runtime.run.thread_id = session.session_id
    runtime.run.turn_id = source.id
    runtime.services.runtime_node_context = lambda: [source]
    coordinator = SubagentCoordinator(
        store=store,
        message_queue=queue,
        index=index,
        job_registry=registry,
    )
    coordinator.bind_session(
        session.session_id,
        lambda: AgentRunner(BlockingPlanner(), ToolRegistry(), job_registry=registry),
        tmp_path,
    )
    try:
        delegated = json.loads(
            coordinator.invoke(
                runtime,
                "delegate_tasks",
                {
                    "subagent_path": "/root/worker",
                    "subagent_task": "wait for config",
                    "context_transfer_strategy": "independent",
                },
            )
        )
        assert delegated == {"thread_path": "/root/worker", "thread_status": "running"}
        thread_id = index.thread_for_path(session.session_id, session.session_id, "/root/worker")
        assert thread_id is not None
        assert started.wait(5), "Subagent did not start"
        updated = coordinator.apply_runtime_config(
            session.session_id,
            thread_id,
            {
                "permission_mode": "workspace_write",
                "running_mode": "plan",
                "model": {"reasoning_effort": "high"},
            },
        )
        assert updated is not None
        assert updated.permission_mode == "workspace_write"
        assert updated.running_mode == "plan"
        assert updated.model["reasoning_effort"] == "high"
        persisted = store.get_node(session.session_id, updated.id)
        assert isinstance(persisted, RuntimeState)
        assert persisted.permission_mode == "workspace_write"
        release.set()
        deadline = monotonic() + 5
        while monotonic() < deadline:
            runtime_thread = store.get_runtime_thread(session.session_id, thread_id)
            if runtime_thread is not None and runtime_thread.running_turn_id is None:
                break
            sleep(0.01)
        else:
            pytest.fail("configured Subagent did not finish")
        assert observed_pending["permission_mode"] == "workspace_write"
        assert observed_pending["running_mode"] == "plan"
        assert observed_pending["model"] == {"reasoning_effort": "high"}
    finally:
        release.set()
        registry.close_all(reason="test complete", timeout=5)
        parent_runner.close()


def test_recover_session_reclaims_preclaimed_delivery_without_duplicate_canonical_input(tmp_path: Path) -> None:
    paths = ClientPaths(tmp_path / "data")
    index = AgentThreadIndex()
    store = SQLiteSessionStore(paths, index)
    queue = MemoryMessageQueue()
    registry = JobRegistry()
    session = store.create_session("recovery")
    source = _finished_source(store, session.session_id)
    child = _agent_create(session.session_id, source, name="worker")
    delivery_id = "delivery-restart"
    child.turn.data[0][0]["delivery_id"] = delivery_id
    turn = RuntimeState.from_dict(child.turn.to_dict())
    child = AgentThreadCreate(child.runtime, child.node, child.context, turn)
    store.create_agent_thread(session.session_id, child)

    envelope = MessageEnvelope(
        delivery_id,
        "agent",
        session.session_id,
        "thread",
        child.node.thread_id,
        session.session_id,
        child.node.thread_id,
        {"content": "task:worker", "references": []},
        (delivery_id,),
    )
    queue.dispatch_agent(envelope)
    assert queue.claim_thread(child.node.thread_id, "dead-worker") is not None

    coordinator = SubagentCoordinator(
        settings=SubagentSettings(max_workers=1),
        store=store,
        message_queue=queue,
        index=index,
        job_registry=registry,
    )
    coordinator.bind_session(
        session.session_id,
        lambda: AgentRunner(_AnswerPlanner(), ToolRegistry(), job_registry=registry),
        tmp_path,
    )

    deadline = monotonic() + 5
    while monotonic() < deadline:
        runtime_thread = store.get_runtime_thread(session.session_id, child.node.thread_id)
        if runtime_thread is not None and runtime_thread.running_turn_id is None:
            break
        sleep(0.01)
    else:
        pytest.fail("recovered Agent Turn did not finish")

    recovered = store.get_node(session.session_id, turn.id)
    assert isinstance(recovered, RuntimeState) and recovered.status == "success"
    assert (
        sum(
            message.get("delivery_id") == delivery_id
            for version in recovered.data
            for message in version
            if message.get("role") == "user"
        )
        == 1
    )
    assert queue.peek_thread(child.node.thread_id) is None
    registry.close_all(reason="test complete", timeout=5)


def test_web_startup_reconciliation_leaves_running_subagent_for_coordinator_recovery(tmp_path: Path) -> None:
    data_root = tmp_path / "web"
    paths = ClientPaths(data_root)
    store = SQLiteSessionStore(paths)
    queue = MemoryMessageQueue()
    session = store.create_session("startup recovery")
    source = _finished_source(store, session.session_id)
    child = _agent_create(session.session_id, source, name="worker")
    store.create_agent_thread(session.session_id, child)

    state = WebAppState(data_root, message_queue=queue)
    try:
        preserved = SQLiteSessionStore(state.paths).get_node(session.session_id, child.turn.id)
        runtime_thread = SQLiteSessionStore(state.paths).get_runtime_thread(
            session.session_id,
            child.node.thread_id,
        )
        assert isinstance(preserved, RuntimeState) and preserved.status == "running"
        assert runtime_thread is not None and runtime_thread.running_turn_id == child.turn.id
    finally:
        state.close()


def test_real_http_sse_redis_subagents_auto_report_and_restart_idle_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    local_sandbox_runtime: None,
) -> None:
    prefix = f"mini-agent:test:agents:{uuid4().hex}"
    client = Redis.from_url("redis://127.0.0.1:6379/0", decode_responses=True)
    try:
        client.ping()
    except Exception as exc:
        client.close()
        pytest.skip(f"real Redis unavailable: {exc}")
    queue = RedisMessageQueue(client, key_prefix=prefix)
    state = WebAppState(tmp_path / "web", message_queue=queue)
    monkeypatch.setattr(
        state,
        "model_config",
        lambda *_args, **_kwargs: ModelConfig("test", "https://example.test/v1", "test"),
    )
    planner = _DelegatingRootPlanner()

    def local_application(_state, *, session_id: str, workspace=None, **_kwargs):
        application = build_application(
            workspace or state.session_workspace(session_id),
            planner_name="rule",
            paths=state.paths,
            job_registry=state.job_registry,
            sandbox_session_id=session_id,
            agent_thread_index=state.agent_thread_index,
            subagent_coordinator=state.subagent_coordinator,
        )
        application.runner.planner = planner
        return application

    monkeypatch.setattr(chat_routes, "build_local_application", local_application)
    try:
        with TestClient(create_app(state)) as http:
            sidebar = http.post("/api/sidebar-threads", json={}).json()
            accepted = http.post(
                "/api/turns",
                json={
                    "id": "turn_agent_root",
                    "session_id": sidebar["session_id"],
                    "thread_id": sidebar["thread_id"],
                    "parent_id": "",
                    "message": {
                        "role": "user",
                        "content": [{"type": "text", "text": "delegate two tasks"}],
                    },
                    "permission_mode": "read_only",
                    "running_mode": "agent",
                },
            )
            assert accepted.status_code == 202, accepted.text
            response = http.get(
                "/api/turns/turn_agent_root/stream",
                params={"session_id": sidebar["session_id"], "thread_id": sidebar["thread_id"]},
            )
            assert response.status_code == 200, response.text
            assert response.text.rstrip().endswith('<SSE id="turn_agent_root" type="success"></SSE>')

            store = SQLiteSessionStore(state.paths, state.agent_thread_index)
            children = store.list_child_thread_nodes(sidebar["session_id"], sidebar["thread_id"])
            assert {child.thread_path for child in children} == {"/root/one", "/root/two"}
            deadline = monotonic() + 5
            while monotonic() < deadline:
                children = store.list_child_thread_nodes(sidebar["session_id"], sidebar["thread_id"])
                if len(children) == 2 and all(child.thread_status == "success" for child in children):
                    break
                sleep(0.01)
            else:
                pytest.fail("delegated Agent Turns did not finish")
            deadline = monotonic() + 5
            while monotonic() < deadline:
                reports = store.list_agent_turn_reports(sidebar["session_id"], states=("delivered",))
                if len(reports) == 2:
                    break
                sleep(0.01)
            else:
                pytest.fail("delegated Agent reports were not delivered")
            root_turn = store.get_node(sidebar["session_id"], "turn_agent_root")
            assert isinstance(root_turn, RuntimeState) and root_turn.status == "success"
            assert "subagent_initial_result" not in json.dumps(root_turn.to_dict())
            report_items = [
                item
                for message in root_turn.data[root_turn.current_data_idx]
                if message.get("role") == "assistant"
                for item in message.get("content", [])
                if item.get("type") == "subagent" and item.get("event") == "agent_report"
            ]
            assert len(report_items) == 2
            assert {item["delivery_id"] for item in report_items} == {report.delivery_id for report in reports}
            assert all(item["status"] == "success" for item in report_items)
            assert queue.peek_thread(sidebar["thread_id"]) is None

            source_runner = AgentRunner(_AnswerPlanner(), ToolRegistry())
            source_runtime = source_runner.new_runtime(task="root", session_id=sidebar["session_id"])
            source_runtime.run.thread_id = sidebar["thread_id"]
            source_runtime.run.turn_id = root_turn.id
            source_runtime.services.runtime_node_context = lambda: [root_turn]
            follow_up = json.loads(
                state.subagent_coordinator.invoke(
                    source_runtime,
                    "send_agent_message",
                    {
                        "source_thread_id": sidebar["thread_id"],
                        "target_thread_path": children[0].thread_path,
                        "subagent_task": "idle follow-up",
                    },
                )
            )
            assert set(follow_up) == {"thread_path", "thread_status"}
            assert follow_up["thread_path"] == children[0].thread_path
            assert follow_up["thread_status"] in {"running", "success"}
            child_thread = store.get_runtime_thread(sidebar["session_id"], children[0].thread_id)
            assert child_thread is not None and child_thread.current_turn_id is not None
            follow_up_turn_id = child_thread.current_turn_id
            deadline = monotonic() + 5
            while monotonic() < deadline:
                child_thread = store.get_runtime_thread(sidebar["session_id"], children[0].thread_id)
                if child_thread is not None and child_thread.running_turn_id is None:
                    break
                sleep(0.01)
            else:
                pytest.fail("idle child follow-up did not finish")
            child_turn = store.get_node(sidebar["session_id"], follow_up_turn_id)
            assert isinstance(child_turn, RuntimeState) and child_turn.status == "success"
            assert child_turn.user_message.get("delivery_id")
            source_runner.close()
    finally:
        state.close()
        keys = list(client.scan_iter(f"{prefix}:*"))
        if keys:
            client.delete(*keys)
        client.close()


def test_real_http_sse_redis_subagents_persist_model_trace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    local_sandbox_runtime: None,
    local_subagent_model: tuple[ModelConfig, list[str]],
) -> None:
    model_config, model_calls = local_subagent_model
    prefix = f"mini-agent:test:agent-trace:{uuid4().hex}"
    client = Redis.from_url("redis://127.0.0.1:6379/0", decode_responses=True)
    try:
        client.ping()
    except Exception as exc:
        client.close()
        pytest.skip(f"real Redis unavailable: {exc}")
    queue = RedisMessageQueue(client, key_prefix=prefix)
    state = WebAppState(tmp_path / "web", message_queue=queue)
    monkeypatch.setattr(state, "model_config", lambda *_args, **_kwargs: model_config)
    planner = _DelegatingRootPlanner()

    def local_application(_state, *, session_id: str, workspace=None, **_kwargs):
        application = build_application(
            workspace or state.session_workspace(session_id),
            planner_name="llm",
            paths=state.paths,
            model_config=model_config,
            job_registry=state.job_registry,
            sandbox_session_id=session_id,
            agent_thread_index=state.agent_thread_index,
            subagent_coordinator=state.subagent_coordinator,
        )
        application.runner.planner = planner
        return application

    monkeypatch.setattr(chat_routes, "build_local_application", local_application)
    try:
        with TestClient(create_app(state)) as http:
            sidebar = http.post("/api/sidebar-threads", json={}).json()
            accepted = http.post(
                "/api/turns",
                json={
                    "id": "turn_agent_trace_root",
                    "session_id": sidebar["session_id"],
                    "thread_id": sidebar["thread_id"],
                    "parent_id": "",
                    "message": {
                        "role": "user",
                        "content": [{"type": "text", "text": "delegate two traced tasks"}],
                    },
                    "permission_mode": "read_only",
                    "running_mode": "agent",
                },
            )
            assert accepted.status_code == 202, accepted.text
            response = http.get(
                "/api/turns/turn_agent_trace_root/stream",
                params={"session_id": sidebar["session_id"], "thread_id": sidebar["thread_id"]},
            )
            assert response.status_code == 200, response.text
            assert response.text.rstrip().endswith('<SSE id="turn_agent_trace_root" type="success"></SSE>')

            store = SQLiteSessionStore(state.paths, state.agent_thread_index)
            children = store.list_child_thread_nodes(sidebar["session_id"], sidebar["thread_id"])
            assert {child.thread_path for child in children} == {"/root/one", "/root/two"}
            deadline = monotonic() + 10
            while monotonic() < deadline:
                runtime_threads = [
                    store.get_runtime_thread(sidebar["session_id"], child.thread_id) for child in children
                ]
                if all(item is not None and item.running_turn_id is None for item in runtime_threads):
                    break
                sleep(0.01)
            else:
                pytest.fail("local HTTP subagents did not finish")

            for child, runtime_thread in zip(children, runtime_threads, strict=True):
                assert runtime_thread is not None and runtime_thread.current_turn_id is not None
                child_turn = store.get_node(sidebar["session_id"], runtime_thread.current_turn_id)
                assert isinstance(child_turn, RuntimeState) and child_turn.status == "success"
                assert child_turn.provider_name == "subagent-trace-local"
                assert child_turn.model["current_model"] == "subagent-trace-test"
                trace = store.load_turn_trace(
                    sidebar["session_id"],
                    child_turn.id,
                    child_turn.current_data_idx,
                )
                assert trace is not None and trace.thread_id == child.thread_id
                assert [entry.role for entry in trace.items] == ["user", "assistant"]
                assert trace.items[-1].item.get("text") == "child answered through local HTTP"
            assert model_calls == ["subagent-trace-test"] * len(children)

            target = children[0]
            follow_up = http.post(
                f"/api/agent-threads/{target.thread_id}/messages",
                json={
                    "session_id": sidebar["session_id"],
                    "content": "run with the selected follow-up model",
                    "model": {
                        "reasoning_effort": "medium",
                        "current_model": "subagent-trace-follow-up",
                        "context_length": 128_000,
                        "output_length": 256,
                        "thinking": "enable",
                        "temperature": 0.0,
                    },
                    "permission_mode": "read_only",
                    "running_mode": "agent",
                },
            )
            assert follow_up.status_code == 202, follow_up.text
            delivery = follow_up.json()
            assert delivery["target_state"] == "started"

            deadline = monotonic() + 10
            while monotonic() < deadline:
                runtime_thread = store.get_runtime_thread(sidebar["session_id"], target.thread_id)
                if (
                    runtime_thread is not None
                    and runtime_thread.running_turn_id is None
                    and runtime_thread.current_turn_id == delivery["turn_id"]
                ):
                    break
                sleep(0.01)
            else:
                pytest.fail("local HTTP Subagent follow-up did not finish")

            follow_up_turn = store.get_node(sidebar["session_id"], delivery["turn_id"])
            assert isinstance(follow_up_turn, RuntimeState) and follow_up_turn.status == "success"
            assert follow_up_turn.provider_name == "subagent-trace-local"
            assert follow_up_turn.model["current_model"] == "subagent-trace-follow-up"
            assert model_calls[-1] == "subagent-trace-follow-up"
            follow_up_trace = store.load_turn_trace(
                sidebar["session_id"],
                follow_up_turn.id,
                follow_up_turn.current_data_idx,
            )
            assert follow_up_trace is not None
            assert follow_up_trace.thread_id == target.thread_id
            assert [entry.role for entry in follow_up_trace.items] == ["user", "assistant"]
            assert follow_up_trace.items[-1].item.get("text") == "child answered through local HTTP"
    finally:
        state.close()
        keys = list(client.scan_iter(f"{prefix}:*"))
        if keys:
            client.delete(*keys)
        client.close()


def test_context_strategies_freeze_share_compact_and_keep_independent_isolated(tmp_path: Path) -> None:
    paths = ClientPaths(tmp_path / "data")
    index = AgentThreadIndex()
    store = SQLiteSessionStore(paths, index)
    queue = MemoryMessageQueue()
    registry = JobRegistry()
    session = store.create_session("contexts")
    root = store.ensure_root_node(session.session_id, id="root")
    source = NodeWriter(store).create(
        RuntimeState.create(
            session_id=session.session_id,
            thread_id=session.session_id,
            id="source",
            parent=root,
            user_content="parent context",
        )
    )
    compaction_calls = 0

    class ParentPlanner(_AnswerPlanner):
        def compact_context(self, _runtime):
            nonlocal compaction_calls
            compaction_calls += 1
            return ContextCompactionResult(True, 2, 1, "one shared summary")

    seen: dict[str, list[str]] = {}

    class RecordingPlanner(_AnswerPlanner):
        def decide(self, runtime):
            seen[runtime.run.task] = [str(message.content or "") for message in runtime.model_messages()]
            return super().decide(runtime)

    parent_runner = AgentRunner(ParentPlanner(), ToolRegistry())
    runtime = parent_runner.new_runtime(task="parent context", session_id=session.session_id)
    runtime.run.thread_id = session.session_id
    runtime.run.turn_id = source.id
    runtime.services.runtime_node_context = lambda: [source]
    coordinator = SubagentCoordinator(
        settings=SubagentSettings(max_workers=4),
        store=store,
        message_queue=queue,
        index=index,
        job_registry=registry,
    )
    coordinator.bind_session(
        session.session_id,
        lambda: AgentRunner(
            RecordingPlanner(),
            ToolRegistry(list(delegation_tools())),
            job_registry=registry,
        ),
        tmp_path,
    )
    specs = (
        ("shared", "share"),
        ("solo", "independent"),
        ("compact-one", "compaction_share"),
        ("compact-two", "compaction_share"),
    )
    for name, strategy in specs:
        result = json.loads(
            coordinator.invoke(
                runtime,
                "delegate_tasks",
                {
                    "subagent_path": f"/root/{name}",
                    "subagent_task": name,
                    "context_transfer_strategy": strategy,
                },
            )
        )
        assert set(result) == {"thread_path", "thread_status"}
        assert result["thread_path"] == f"/root/{name}"
    assert compaction_calls == 2
    expected_tasks = {"shared", "solo", "compact-one", "compact-two"}
    deadline = monotonic() + 5
    while monotonic() < deadline and not expected_tasks.issubset(seen):
        sleep(0.01)
    assert expected_tasks.issubset(seen)
    assert seen["solo"] == ["solo"]
    assert seen["shared"] == ["parent context", "shared"]
    assert seen["compact-one"] == [f"{CHECKPOINT_PREAMBLE}\n\none shared summary", "compact-one"]
    assert seen["compact-two"] == [f"{CHECKPOINT_PREAMBLE}\n\none shared summary", "compact-two"]
    registry.close_all(reason="test complete", timeout=5)
    parent_runner.close()


def test_compaction_share_failure_falls_back_to_independent(tmp_path: Path) -> None:
    index = AgentThreadIndex()
    store = SQLiteSessionStore(ClientPaths(tmp_path / "data"), index)
    queue = MemoryMessageQueue()
    registry = JobRegistry()
    session = store.create_session("compaction fallback")
    source = _finished_source(store, session.session_id)
    seen: list[str] = []

    class FailingCompactionPlanner(_AnswerPlanner):
        def compact_context(self, _runtime):
            raise RuntimeError("local compaction failed")

    class RecordingPlanner(_AnswerPlanner):
        def decide(self, child_runtime):
            seen.extend(str(message.content or "") for message in child_runtime.model_messages())
            return super().decide(child_runtime)

    parent_runner = AgentRunner(FailingCompactionPlanner(), ToolRegistry())
    runtime = parent_runner.new_runtime(task="parent", session_id=session.session_id)
    runtime.run.thread_id = session.session_id
    runtime.run.turn_id = source.id
    runtime.services.runtime_node_context = lambda: [source]
    coordinator = SubagentCoordinator(
        store=store,
        message_queue=queue,
        index=index,
        job_registry=registry,
    )
    coordinator.bind_session(
        session.session_id,
        lambda: AgentRunner(RecordingPlanner(), ToolRegistry(), job_registry=registry),
        tmp_path,
    )
    try:
        coordinator.invoke(
            runtime,
            "delegate_tasks",
            {
                "subagent_path": "/root/fallback",
                "subagent_task": "fallback",
                "context_transfer_strategy": "compaction_share",
            },
        )
        child_id = index.thread_for_path(session.session_id, session.session_id, "/root/fallback")
        assert child_id is not None
        context = store.get_thread_context(session.session_id, child_id)
        assert context is not None
        assert context.requested_strategy == "compaction_share"
        assert context.effective_strategy == "independent"
        deadline = monotonic() + 5
        while monotonic() < deadline and not seen:
            sleep(0.01)
        assert seen == ["fallback"]
    finally:
        registry.close_all(reason="test complete", timeout=5)
        parent_runner.close()


def test_delegate_paths_source_auth_and_recursive_get_thread_node(tmp_path: Path) -> None:
    paths = ClientPaths(tmp_path / "data")
    index = AgentThreadIndex()
    store = SQLiteSessionStore(paths, index)
    queue = MemoryMessageQueue()
    registry = JobRegistry()
    session = store.create_session("paths")
    source = _finished_source(store, session.session_id)
    release = Event()

    class BlockingPlanner:
        name = "blocking-paths"

        def decide(self, _runtime):
            assert release.wait(5)
            return AssistantMessage(content="released")

    parent_runner = AgentRunner(_AnswerPlanner(), ToolRegistry())
    runtime = parent_runner.new_runtime(task="parent", session_id=session.session_id)
    runtime.run.thread_id = session.session_id
    runtime.run.turn_id = source.id
    runtime.services.runtime_node_context = lambda: [source]
    coordinator = SubagentCoordinator(
        settings=SubagentSettings(max_workers=3),
        store=store,
        message_queue=queue,
        index=index,
        job_registry=registry,
    )
    coordinator.bind_session(
        session.session_id,
        lambda: AgentRunner(BlockingPlanner(), ToolRegistry(), job_registry=registry),
        tmp_path,
    )
    try:
        for path in ("/root/parent", "/root/parent/grandchild", "/root/other"):
            result = json.loads(
                coordinator.invoke(
                    runtime,
                    "delegate_tasks",
                    {
                        "subagent_path": path,
                        "subagent_task": path.rsplit("/", 1)[-1],
                        "context_transfer_strategy": "independent",
                    },
                )
            )
            assert result == {"thread_path": path, "thread_status": "running"}

        with pytest.raises(ToolError, match="already exists"):
            coordinator.invoke(
                runtime,
                "delegate_tasks",
                {
                    "subagent_path": "/root/parent",
                    "subagent_task": "duplicate",
                    "context_transfer_strategy": "independent",
                },
            )
        with pytest.raises(ToolError, match="parent path does not exist"):
            coordinator.invoke(
                runtime,
                "delegate_tasks",
                {
                    "subagent_path": "/root/missing/leaf",
                    "subagent_task": "missing",
                    "context_transfer_strategy": "independent",
                },
            )
        with pytest.raises(ToolError, match="does not match"):
            coordinator.invoke(
                runtime,
                "delegate_tasks",
                {
                    "source_thread_id": "thread_spoofed",
                    "subagent_path": "/root/spoofed",
                    "subagent_task": "spoofed",
                    "context_transfer_strategy": "independent",
                },
            )

        descendants = store.list_descendant_thread_nodes(session.session_id, session.session_id)
        listed = json.loads(coordinator.invoke(runtime, "get_thread_node", {}))
        assert [item["thread_path"] for item in listed] == [item.thread_path for item in descendants]
        assert all(set(item) == {"thread_path", "thread_status", "task_result"} for item in listed)
        assert json.loads(
            coordinator.invoke(
                runtime,
                "get_thread_node",
                {"target_thread_path": "/root/parent/grandchild"},
            )
        ) == [
            {
                "thread_path": "/root/parent/grandchild",
                "thread_status": "running",
                "task_result": "",
            }
        ]
        assert json.loads(coordinator.invoke(runtime, "get_thread_node", {"target_thread_path": "/root"})) == [
            {"thread_path": "/root", "thread_status": "success", "task_result": ""}
        ]

        parent_id = index.thread_for_path(session.session_id, session.session_id, "/root/parent")
        assert parent_id is not None
        parent_turn_id = index.head_for_thread(parent_id)
        parent_turn = store.get_node(session.session_id, parent_turn_id or "")
        assert isinstance(parent_turn, RuntimeState)
        child_runner = AgentRunner(_AnswerPlanner(), ToolRegistry())
        child_runtime = child_runner.new_runtime(task="parent", session_id=session.session_id)
        child_runtime.run.thread_id = parent_id
        child_runtime.run.turn_id = parent_turn.id
        child_runtime.services.runtime_node_context = lambda: [parent_turn]
        with pytest.raises(ToolError, match="descendants"):
            coordinator.invoke(
                child_runtime,
                "get_thread_node",
                {"target_thread_path": "/root/other"},
            )
        child_runner.close()
    finally:
        release.set()
        registry.close_all(reason="test complete", timeout=5)
        parent_runner.close()


def test_send_agent_message_reference_boundaries_and_symlink_escape(tmp_path: Path) -> None:
    session_workspace = (tmp_path / "session").resolve()
    project_workspace = (tmp_path / "project").resolve()
    outside_workspace = (tmp_path / "outside").resolve()
    for path in (session_workspace, project_workspace, outside_workspace):
        path.mkdir()
    session_file = session_workspace / "session.txt"
    project_file = project_workspace / "project.txt"
    outside_file = outside_workspace / "outside.txt"
    session_file.write_text("session", encoding="utf-8")
    project_file.write_text("project", encoding="utf-8")
    outside_file.write_text("outside", encoding="utf-8")

    index = AgentThreadIndex()
    store = SQLiteSessionStore(ClientPaths(tmp_path / "data"), index)
    queue = MemoryMessageQueue()
    registry = JobRegistry()
    session = store.create_session("references")
    root = store.ensure_root_node(session.session_id, id="root")
    writer = NodeWriter(store)
    source = writer.create(
        RuntimeState.create(
            session_id=session.session_id,
            thread_id=session.session_id,
            id="source",
            parent=root,
            user_content="parent",
            cwd=str(session_workspace),
            project_cwd=str(project_workspace),
        )
    )
    source = writer.finalize(source, "success")
    child = _agent_create(session.session_id, source, name="worker")
    child.turn.cwd = str(session_workspace)
    child.turn.project_cwd = str(project_workspace)
    child = AgentThreadCreate(child.runtime, child.node, child.context, RuntimeState.from_dict(child.turn.to_dict()))
    store.create_agent_thread(session.session_id, child)

    parent_runner = AgentRunner(_AnswerPlanner(), ToolRegistry())
    runtime = parent_runner.new_runtime(task="parent", session_id=session.session_id)
    runtime.run.thread_id = session.session_id
    runtime.run.turn_id = source.id
    runtime.services.runtime_node_context = lambda: [source]
    coordinator = SubagentCoordinator(
        store=store,
        message_queue=queue,
        index=index,
        job_registry=registry,
    )
    try:
        result = json.loads(
            coordinator.invoke(
                runtime,
                "send_agent_message",
                {
                    "target_thread_path": "/root/worker",
                    "subagent_task": "inspect files",
                    "references": [{"path": str(session_file)}, {"path": str(project_file)}],
                },
            )
        )
        assert result == {"thread_path": "/root/worker", "thread_status": "running"}
        envelope = queue.peek_thread(child.node.thread_id)
        assert envelope is not None
        assert envelope.references == ({"path": str(session_file)}, {"path": str(project_file)})

        invalid_references = (
            ({"path": "relative.txt"}, "absolute"),
            ({"path": str(tmp_path / "missing.txt")}, "does not exist"),
            ({"path": str(session_workspace)}, "not a file"),
            ({"path": str(outside_file)}, "outside"),
        )
        for reference, message in invalid_references:
            with pytest.raises(ToolError, match=message):
                coordinator.invoke(
                    runtime,
                    "send_agent_message",
                    {
                        "target_thread_path": "/root/worker",
                        "subagent_task": "invalid reference",
                        "references": [reference],
                    },
                )

        link = session_workspace / "escape.txt"
        try:
            link.symlink_to(outside_file)
        except OSError:
            link = None
        if link is not None:
            with pytest.raises(ToolError, match="outside"):
                coordinator.invoke(
                    runtime,
                    "send_agent_message",
                    {
                        "target_thread_path": "/root/worker",
                        "subagent_task": "symlink escape",
                        "references": [{"path": str(link)}],
                    },
                )
        with pytest.raises(ToolError, match="cannot send a message to itself"):
            coordinator.invoke(
                runtime,
                "send_agent_message",
                {"target_thread_path": "/root", "subagent_task": "self"},
            )
    finally:
        registry.close_all(reason="test complete", timeout=5)
        parent_runner.close()


def test_status_control_transitions_are_direct_child_only_and_reuse_the_paused_turn(tmp_path: Path) -> None:
    index = AgentThreadIndex()
    store = SQLiteSessionStore(ClientPaths(tmp_path / "data"), index)
    queue = MemoryMessageQueue()
    registry = JobRegistry()
    session = store.create_session("status control")
    source = _finished_source(store, session.session_id)
    tick_entered = Event()
    tick_gate = Event()

    def tick() -> str:
        tick_entered.set()
        assert tick_gate.wait(5), "test did not release the safe-boundary tool"
        return "tick"

    tick_tool = Tool(
        "tick",
        "Pause at a deterministic safe boundary.",
        tick,
        {"type": "object", "properties": {}, "additionalProperties": False},
    )

    class SpinningPlanner:
        name = "spinning"

        def __init__(self) -> None:
            self.calls = 0

        def decide(self, _runtime):
            self.calls += 1
            return AssistantMessage(
                tool_messages=[ToolMessage(name="tick", call_id=f"tick_{self.calls}", arguments={})]
            )

    parent_runner = AgentRunner(_AnswerPlanner(), ToolRegistry())
    runtime = parent_runner.new_runtime(task="parent", session_id=session.session_id)
    runtime.run.thread_id = session.session_id
    runtime.run.turn_id = source.id
    runtime.services.runtime_node_context = lambda: [source]
    coordinator = SubagentCoordinator(
        store=store,
        message_queue=queue,
        index=index,
        job_registry=registry,
    )
    coordinator.bind_session(
        session.session_id,
        lambda: AgentRunner(
            SpinningPlanner(),
            ToolRegistry([tick_tool]),
            max_tool_calls=1_000,
            job_registry=registry,
        ),
        tmp_path,
    )
    try:
        coordinator.invoke(
            runtime,
            "delegate_tasks",
            {
                "subagent_path": "/root/worker",
                "subagent_task": "spin",
                "context_transfer_strategy": "independent",
            },
        )
        assert tick_entered.wait(5), "Subagent did not enter its first tool boundary"
        child_id = index.thread_for_path(session.session_id, session.session_id, "/root/worker")
        assert child_id is not None
        initial_turn_id = index.head_for_thread(child_id)
        assert initial_turn_id is not None

        with ThreadPoolExecutor(max_workers=1) as pool:
            pausing = pool.submit(
                coordinator.invoke,
                runtime,
                "set_thread_node_status",
                {"target_thread_path": "/root/worker", "thread_status": "paused"},
            )
            deadline = monotonic() + 5
            while monotonic() < deadline:
                with coordinator._state_lock:
                    if child_id in coordinator._status_controls:
                        break
                sleep(0.01)
            else:
                pytest.fail("pause control was not registered")
            with pytest.raises(ToolError, match="pending status change"):
                coordinator.invoke(
                    runtime,
                    "set_thread_node_status",
                    {"target_thread_path": "/root/worker", "thread_status": "success"},
                )
            tick_gate.set()
            assert json.loads(pausing.result(timeout=5)) == {
                "thread_path": "/root/worker",
                "thread_status": "paused",
            }

        assert index.head_for_thread(child_id) == initial_turn_id
        with pytest.raises(ToolError, match="paused to paused"):
            coordinator.invoke(
                runtime,
                "set_thread_node_status",
                {"target_thread_path": "/root/worker", "thread_status": "paused"},
            )

        tick_gate.clear()
        tick_entered.clear()
        assert json.loads(
            coordinator.invoke(
                runtime,
                "set_thread_node_status",
                {"target_thread_path": "/root/worker", "thread_status": "running"},
            )
        ) == {"thread_path": "/root/worker", "thread_status": "running"}
        assert index.head_for_thread(child_id) == initial_turn_id
        assert tick_entered.wait(5), "resumed Subagent did not re-enter its tool boundary"

        with ThreadPoolExecutor(max_workers=1) as pool:
            pausing_again = pool.submit(
                coordinator.invoke,
                runtime,
                "set_thread_node_status",
                {"target_thread_path": "/root/worker", "thread_status": "paused"},
            )
            deadline = monotonic() + 5
            while monotonic() < deadline:
                with coordinator._state_lock:
                    if child_id in coordinator._status_controls:
                        break
                sleep(0.01)
            else:
                pytest.fail("second pause control was not registered")
            tick_gate.set()
            assert json.loads(pausing_again.result(timeout=5))["thread_status"] == "paused"

        tick_gate.clear()
        tick_entered.clear()
        assert json.loads(
            coordinator.invoke(
                runtime,
                "send_agent_message",
                {"target_thread_path": "/root/worker", "subagent_task": "resume with task"},
            )
        ) == {"thread_path": "/root/worker", "thread_status": "running"}
        assert index.head_for_thread(child_id) == initial_turn_id
        assert tick_entered.wait(5), "message-resumed Subagent did not reach a safe boundary"

        with ThreadPoolExecutor(max_workers=1) as pool:
            completing = pool.submit(
                coordinator.invoke,
                runtime,
                "set_thread_node_status",
                {"target_thread_path": "/root/worker", "thread_status": "success"},
            )
            deadline = monotonic() + 5
            while monotonic() < deadline:
                with coordinator._state_lock:
                    if child_id in coordinator._status_controls:
                        break
                sleep(0.01)
            else:
                pytest.fail("success control was not registered")
            tick_gate.set()
            assert json.loads(completing.result(timeout=5)) == {
                "thread_path": "/root/worker",
                "thread_status": "success",
            }

        assert index.head_for_thread(child_id) == initial_turn_id
        final_turn = store.get_node(session.session_id, initial_turn_id)
        assert isinstance(final_turn, RuntimeState)
        assert any(
            item.get("type") == "text" and item.get("text") == "resume with task"
            for message in final_turn.data[final_turn.current_data_idx]
            if message.get("role") == "user"
            for item in message.get("content", [])
        )
        with pytest.raises(ToolError, match="success to running"):
            coordinator.invoke(
                runtime,
                "set_thread_node_status",
                {"target_thread_path": "/root/worker", "thread_status": "running"},
            )
    finally:
        tick_gate.set()
        registry.close_all(reason="test complete", timeout=5)
        parent_runner.close()


def test_running_status_control_timeout_revokes_unclaimed_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    index = AgentThreadIndex()
    store = SQLiteSessionStore(ClientPaths(tmp_path / "data"), index)
    queue = MemoryMessageQueue()
    registry = JobRegistry()
    session = store.create_session("status timeout")
    source = _finished_source(store, session.session_id)
    child = _agent_create(session.session_id, source, name="worker")
    store.create_agent_thread(session.session_id, child)

    class ImmediateTimeoutEvent:
        def wait(self, _timeout: float | None = None) -> bool:
            return False

        def set(self) -> None:
            return None

    monkeypatch.setattr("backend.runtime.subagent.tool_actions.Event", ImmediateTimeoutEvent)
    parent_runner = AgentRunner(_AnswerPlanner(), ToolRegistry())
    runtime = parent_runner.new_runtime(task="parent", session_id=session.session_id)
    runtime.run.thread_id = session.session_id
    runtime.run.turn_id = source.id
    runtime.services.runtime_node_context = lambda: [source]
    coordinator = SubagentCoordinator(
        store=store,
        message_queue=queue,
        index=index,
        job_registry=registry,
    )
    try:
        with pytest.raises(ToolError, match="request was revoked and current status is running"):
            coordinator.invoke(
                runtime,
                "set_thread_node_status",
                {"target_thread_path": "/root/worker", "thread_status": "paused"},
            )
        assert coordinator._status_controls == {}
        current = store.get_thread_node(session.session_id, child.node.thread_id)
        assert current is not None and current.thread_status == "running"
    finally:
        registry.close_all(reason="test complete", timeout=5)
        parent_runner.close()


def test_running_message_uses_steering_without_creating_another_turn(tmp_path: Path) -> None:
    index = AgentThreadIndex()
    store = SQLiteSessionStore(ClientPaths(tmp_path / "data"), index)
    queue = MemoryMessageQueue()
    registry = JobRegistry()
    session = store.create_session("running steering")
    source = _finished_source(store, session.session_id)
    tool_entered = Event()
    release_tool = Event()
    saw_message = Event()

    def hold() -> str:
        tool_entered.set()
        assert release_tool.wait(5)
        return "held"

    hold_tool = Tool(
        "hold",
        "Hold the first model cycle while a steering message arrives.",
        hold,
        {"type": "object", "properties": {}, "additionalProperties": False},
    )

    class SteeringPlanner:
        name = "steering"

        def decide(self, child_runtime):
            messages = [str(message.content or "") for message in child_runtime.model_messages()]
            if any("steered task" in message for message in messages):
                saw_message.set()
                return AssistantMessage(content="handled steering")
            return AssistantMessage(tool_messages=[ToolMessage(name="hold", call_id="hold_once", arguments={})])

    parent_runner = AgentRunner(_AnswerPlanner(), ToolRegistry())
    runtime = parent_runner.new_runtime(task="parent", session_id=session.session_id)
    runtime.run.thread_id = session.session_id
    runtime.run.turn_id = source.id
    runtime.services.runtime_node_context = lambda: [source]
    coordinator = SubagentCoordinator(
        store=store,
        message_queue=queue,
        index=index,
        job_registry=registry,
    )
    coordinator.bind_session(
        session.session_id,
        lambda: AgentRunner(SteeringPlanner(), ToolRegistry([hold_tool]), job_registry=registry),
        tmp_path,
    )
    try:
        coordinator.invoke(
            runtime,
            "delegate_tasks",
            {
                "subagent_path": "/root/worker",
                "subagent_task": "wait",
                "context_transfer_strategy": "independent",
            },
        )
        assert tool_entered.wait(5)
        child_id = index.thread_for_path(session.session_id, session.session_id, "/root/worker")
        assert child_id is not None
        turn_id = index.head_for_thread(child_id)
        assert turn_id is not None
        assert json.loads(
            coordinator.invoke(
                runtime,
                "send_agent_message",
                {"target_thread_path": "/root/worker", "subagent_task": "steered task"},
            )
        ) == {"thread_path": "/root/worker", "thread_status": "running"}
        release_tool.set()
        assert saw_message.wait(5), "running Agent did not consume its explicit steering message"
        deadline = monotonic() + 5
        while monotonic() < deadline:
            node = store.get_thread_node(session.session_id, child_id)
            if node is not None and node.thread_status == "success":
                break
            sleep(0.01)
        else:
            pytest.fail("steered Agent did not finish")
        assert index.head_for_thread(child_id) == turn_id
        assert queue.peek_thread(child_id) is None
    finally:
        release_tool.set()
        registry.close_all(reason="test complete", timeout=5)
        parent_runner.close()


def test_failed_result_retry_and_success_without_text(tmp_path: Path) -> None:
    index = AgentThreadIndex()
    store = SQLiteSessionStore(ClientPaths(tmp_path / "data"), index)
    queue = MemoryMessageQueue()
    registry = JobRegistry()
    session = store.create_session("results")
    source = _finished_source(store, session.session_id)

    class ResultPlanner:
        name = "results"

        def decide(self, child_runtime):
            if child_runtime.run.task == "fail naturally":
                raise RuntimeError("natural failure detail")
            if child_runtime.run.task == "no text":
                return AssistantMessage(content="")
            return AssistantMessage(content=f"recovered:{child_runtime.run.task}")

    parent_runner = AgentRunner(_AnswerPlanner(), ToolRegistry())
    runtime = parent_runner.new_runtime(task="parent", session_id=session.session_id)
    runtime.run.thread_id = session.session_id
    runtime.run.turn_id = source.id
    runtime.services.runtime_node_context = lambda: [source]
    coordinator = SubagentCoordinator(
        settings=SubagentSettings(max_workers=2),
        store=store,
        message_queue=queue,
        index=index,
        job_registry=registry,
    )
    coordinator.bind_session(
        session.session_id,
        lambda: AgentRunner(ResultPlanner(), ToolRegistry(), job_registry=registry),
        tmp_path,
    )
    try:
        for path, task in (("/root/failing", "fail naturally"), ("/root/empty", "no text")):
            coordinator.invoke(
                runtime,
                "delegate_tasks",
                {
                    "subagent_path": path,
                    "subagent_task": task,
                    "context_transfer_strategy": "independent",
                },
            )
        deadline = monotonic() + 5
        while monotonic() < deadline:
            nodes = store.list_descendant_thread_nodes(session.session_id, session.session_id)
            if len(nodes) == 2 and all(node.thread_status != "running" for node in nodes):
                break
            sleep(0.01)
        else:
            pytest.fail("result Agents did not settle")

        listed = json.loads(coordinator.invoke(runtime, "get_thread_node", {}))
        by_path = {item["thread_path"]: item for item in listed}
        failed = by_path["/root/failing"]
        assert failed["thread_status"] == "failed"
        assert failed["task_result"].startswith("natural failure detail\n\n")
        assert failed["task_result"].endswith(
            "This agent's turn failed. If you still need this agent, use the send_agent_message tool to give it another task."
        )
        assert by_path["/root/empty"] == {
            "thread_path": "/root/empty",
            "thread_status": "success",
            "task_result": "",
        }

        failing_id = index.thread_for_path(session.session_id, session.session_id, "/root/failing")
        assert failing_id is not None
        failed_turn_id = index.head_for_thread(failing_id)
        retry = json.loads(
            coordinator.invoke(
                runtime,
                "send_agent_message",
                {"target_thread_path": "/root/failing", "subagent_task": "retry task"},
            )
        )
        assert set(retry) == {"thread_path", "thread_status"}
        deadline = monotonic() + 5
        while monotonic() < deadline:
            node = store.get_thread_node(session.session_id, failing_id)
            if node is not None and node.thread_status == "success":
                break
            sleep(0.01)
        else:
            pytest.fail("failed Agent retry did not finish")
        assert index.head_for_thread(failing_id) != failed_turn_id
    finally:
        registry.close_all(reason="test complete", timeout=5)
        parent_runner.close()
