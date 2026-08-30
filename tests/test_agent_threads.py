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
    RuntimeRootState,
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
from backend.tools import ToolError, ToolRegistry, delegation_tools


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
        if self.calls == 1:
            return AssistantMessage(
                tool_messages=[
                    ToolMessage(
                        name="delegate_tasks",
                        call_id="delegate_http_children",
                        arguments={
                            "subagent_count": 2,
                            "subagent_name": ["one", "two"],
                            "subagent_tasks": ["first", "second"],
                            "context_transfer_strategy": ["independent", "independent"],
                        },
                    )
                ]
            )
        messages = [str(message.content or "") for message in runtime.model_messages()]
        received = sum('"type": "subagent_initial_result"' in content for content in messages)
        if received >= 2:
            return AssistantMessage(content="root received both Agent results")
        return AssistantMessage(
            tool_messages=[
                ToolMessage(
                    name="list_current_node_sub_thread",
                    call_id=f"wait_for_agents_{self.calls}",
                    arguments={},
                )
            ]
        )


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
            f"task:{name}",
            "opening",
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
            f"task:{name}",
            "opening",
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
    store.create_agent_threads(session.session_id, [child])

    assert index.threads_for_session(session.session_id) == frozenset({session.session_id, child.node.thread_id})
    assert index.session_for_thread(child.node.thread_id) == session.session_id
    assert index.head_for_thread(child.node.thread_id) == child.turn.id
    assert index.thread_for_path(session.session_id, session.session_id, "/root/worker") == child.node.thread_id
    assert index.path_for_thread(child.node.thread_id) == "/root/worker"

    rebuilt = AgentThreadIndex()
    rebuilt.rebuild(SQLiteSessionStore(paths))
    assert rebuilt.threads_for_session(session.session_id) == index.threads_for_session(session.session_id)
    assert rebuilt.path_for_thread(child.node.thread_id) == "/root/worker"


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
            store.create_agent_threads(sidebar["session_id"], [child])
            grandchild = _nested_agent_create(
                sidebar["session_id"],
                child.turn,
                name="nested",
                parent_path=child.node.thread_path,
                depth=2,
            )
            store.create_agent_threads(sidebar["session_id"], [grandchild])

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
            store.create_agent_threads(sidebar["session_id"], [fork_child])

            children = client.get(
                f"/api/agent-threads/{sidebar['session_id']}/children",
                params={"session_id": sidebar["session_id"]},
            )
            assert children.status_code == 200
            assert children.json() == [
                {
                    "thread_id": child.node.thread_id,
                    "thread_path": "/root/worker",
                    "thread_task": "task:worker",
                    "thread_status": "opening",
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
                    "thread_task": "task:worker",
                    "thread_status": "opening",
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
                    "thread_task": "task:nested",
                    "thread_status": "opening",
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
                    "references": [{"source": "project", "path": "README.md"}],
                    "permission_mode": "workspace_write",
                    "running_mode": "plan",
                },
            )
            assert response.status_code == 202, response.text
            assert response.json()["target_state"] == "running"
            envelope = queue.peek_thread(child.node.thread_id)
            assert envelope is not None
            assert envelope.source_thread_id == sidebar["session_id"]
            assert envelope.references == ({"source": "project", "path": "README.md"},)
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
            with pytest.raises(ToolError, match="same Agent tree"):
                state.subagent_coordinator.invoke(
                    source_runtime,
                    "send_agent_message",
                    {
                        "target_thread_id": fork_child.node.thread_id,
                        "subagent_tasks": "cross-tree message",
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

            store.update_thread_status(sidebar["session_id"], grandchild.node.thread_id, "closed")
            closed = client.post(
                f"/api/agent-threads/{grandchild.node.thread_id}/messages",
                json={"session_id": sidebar["session_id"], "content": "rejected"},
            )
            assert closed.status_code == 409

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
                    "subagent_count": 1,
                    "subagent_name": ["worker"],
                    "subagent_tasks": ["fork-only task"],
                    "context_transfer_strategy": ["independent"],
                },
            )
        )
        child_id = delegated["subagents"][0]["thread_id"]
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
            store.create_agent_threads(sidebar["session_id"], [child])

            response = client.post(
                f"/api/agent-threads/{child.node.thread_id}/messages",
                json={"session_id": sidebar["session_id"], "content": "must fail closed"},
            )
            assert response.status_code == 503
            assert response.json() == {"detail": "message_queue_unavailable"}
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
            store.create_agent_threads(sidebar["session_id"], [child])
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


def _create_v13_database(path: Path, session_id: str, *, orphan_sidebar: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    timestamp = utc_iso()
    root = RuntimeRootState.create(session_id, id="root")
    main = RuntimeState.create(
        session_id=session_id,
        thread_id=session_id,
        id="main_turn",
        parent=root,
        user_content="first task",
    )
    main.status = "success"
    fork = RuntimeState.from_dict(main.to_dict())
    fork.id = "fork_turn"
    fork.thread_id = "thread_fork"
    fork.compaction_id = fork.id
    fork = RuntimeState.from_dict(fork.to_dict())
    side = RuntimeState.from_dict(fork.to_dict())
    side.id = "side_turn"
    side.thread_id = "thread_side"
    side.compaction_id = side.id
    side = RuntimeState.from_dict(side.to_dict())
    child = RuntimeState.create(
        session_id=session_id,
        thread_id="thread_child",
        id="child_turn",
        parent=main,
        user_content="task:worker",
    )
    child.status = "success"
    child = RuntimeState.from_dict(child.to_dict())
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE store_metadata(session_id TEXT PRIMARY KEY,schema_version INTEGER NOT NULL CHECK(schema_version=13),created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
            CREATE TABLE json_objects(session_id TEXT NOT NULL,namespace TEXT NOT NULL,object_id TEXT NOT NULL,payload_json TEXT NOT NULL,updated_at TEXT NOT NULL,PRIMARY KEY(session_id,namespace,object_id));
            CREATE TABLE workspace_files(session_id TEXT NOT NULL,relative_path TEXT NOT NULL,size INTEGER NOT NULL,sha256 TEXT NOT NULL,mtime_ns INTEGER NOT NULL,PRIMARY KEY(session_id,relative_path));
            CREATE TABLE sandbox_approvals(request_hash TEXT PRIMARY KEY,session_id TEXT NOT NULL,command_hash TEXT NOT NULL,cwd_hash TEXT NOT NULL,permission_target TEXT NOT NULL,network_target_hash TEXT NOT NULL,command_summary TEXT NOT NULL,cwd_summary TEXT NOT NULL,created_at TEXT NOT NULL);
            CREATE TABLE runtime_threads(session_id TEXT NOT NULL,thread_id TEXT PRIMARY KEY,origin_kind TEXT NOT NULL,current_turn_id TEXT,running_turn_id TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
            CREATE INDEX runtime_threads_session_idx ON runtime_threads(session_id,created_at,thread_id);
            CREATE TABLE thread_nodes(session_id TEXT NOT NULL,thread_id TEXT PRIMARY KEY REFERENCES runtime_threads(thread_id) ON DELETE CASCADE,parent_thread_id TEXT REFERENCES thread_nodes(thread_id),thread_path TEXT NOT NULL,thread_task TEXT NOT NULL,thread_status TEXT NOT NULL,depth INTEGER NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,UNIQUE(session_id,thread_path));
            CREATE INDEX thread_nodes_parent_idx ON thread_nodes(session_id,parent_thread_id,created_at,thread_id);
            CREATE TABLE thread_contexts(thread_id TEXT PRIMARY KEY REFERENCES thread_nodes(thread_id) ON DELETE CASCADE,requested_strategy TEXT NOT NULL,effective_strategy TEXT NOT NULL,source_turn_id TEXT NOT NULL,source_data_idx INTEGER NOT NULL,snapshot_json TEXT,summary TEXT);
            """
        )
        connection.execute(
            "INSERT INTO store_metadata VALUES (?,13,?,?)",
            (session_id, timestamp, timestamp),
        )
        session_payload = {
            "session_id": session_id,
            "title": "migrated",
            "created_at": timestamp,
            "updated_at": timestamp,
            "title_is_custom": False,
        }
        sidebar_payloads = (
            {
                "thread_id": session_id,
                "session_id": session_id,
                "title": "migrated",
                "created_at": timestamp,
                "updated_at": timestamp,
                "archived_at": None,
                "deleted_at": None,
                "title_is_custom": False,
            },
            {
                "thread_id": "thread_fork",
                "session_id": session_id,
                "title": "migrated fork",
                "created_at": timestamp,
                "updated_at": timestamp,
                "archived_at": None,
                "deleted_at": None,
                "title_is_custom": False,
            },
        )
        for namespace, object_id, payload in (
            ("session", session_id, session_payload),
            ("runtime_node", root.id, root.to_dict()),
            ("runtime_node", main.id, main.to_dict()),
            ("runtime_node", fork.id, fork.to_dict()),
            ("runtime_node", side.id, side.to_dict()),
            ("runtime_node", child.id, child.to_dict()),
            *(("sidebar_thread", item["thread_id"], item) for item in sidebar_payloads),
        ):
            connection.execute(
                "INSERT INTO json_objects VALUES (?,?,?,?,?)",
                (session_id, namespace, object_id, json.dumps(payload), timestamp),
            )
        for values in (
            (session_id, session_id, "main", main.id, None, timestamp, timestamp),
            (session_id, fork.thread_id, "fork", fork.id, None, timestamp, timestamp),
            (session_id, side.thread_id, "fork", side.id, None, timestamp, timestamp),
            (session_id, child.thread_id, "subagent", child.id, None, timestamp, timestamp),
        ):
            connection.execute("INSERT INTO runtime_threads VALUES (?,?,?,?,?,?,?)", values)
        connection.execute(
            "INSERT INTO thread_nodes VALUES (?,?,NULL,'/root','first task','opening',0,?,?)",
            (session_id, session_id, timestamp, timestamp),
        )
        connection.execute(
            "INSERT INTO thread_nodes VALUES (?,?,?,'/root/worker','task:worker','opening',1,?,?)",
            (session_id, child.thread_id, session_id, timestamp, timestamp),
        )
        connection.execute(
            "INSERT INTO thread_contexts VALUES (?,?,?,?,?,?,?)",
            (child.thread_id, "independent", "independent", main.id, 0, None, None),
        )
        if orphan_sidebar:
            orphan = {**sidebar_payloads[1], "thread_id": "thread_orphan", "title": "orphan"}
            connection.execute(
                "INSERT INTO json_objects VALUES (?,?,?,?,?)",
                (session_id, "sidebar_thread", orphan["thread_id"], json.dumps(orphan), timestamp),
            )


def test_v13_migrates_to_v14_with_independent_sidebar_roots_and_excludes_side_chat(tmp_path: Path) -> None:
    paths = ClientPaths(tmp_path / "data")
    paths.ensure()
    session_id = "session_migrate"
    database = paths.session_db(session_id)
    _create_v13_database(database, session_id)

    store = SQLiteSessionStore(paths)
    threads = store.list_runtime_threads(session_id)
    assert {(item.thread_id, item.origin_kind) for item in threads} == {
        (session_id, "main"),
        ("thread_fork", "fork"),
        ("thread_side", "fork"),
        ("thread_child", "subagent"),
    }
    main_root = store.get_thread_node(session_id, session_id)
    child = store.get_thread_node(session_id, "thread_child")
    fork_root = store.get_thread_node(session_id, "thread_fork")
    assert main_root is not None and main_root.root_thread_id == session_id
    assert child is not None and child.root_thread_id == session_id
    assert fork_root is not None and fork_root.root_thread_id == "thread_fork"
    assert fork_root.thread_path == "/root" and fork_root.depth == 0
    assert store.list_child_thread_nodes(session_id, "thread_fork") == []
    assert store.get_thread_node(session_id, "thread_side") is None

    fork_turn = store.get_node(session_id, "fork_turn")
    assert isinstance(fork_turn, RuntimeState)
    fork_child = _agent_create(session_id, fork_turn, name="worker", root_thread_id="thread_fork")
    store.create_agent_threads(session_id, [fork_child])
    assert store.get_thread_node(session_id, fork_child.node.thread_id).thread_path == "/root/worker"
    assert store.get_thread_node(session_id, "thread_child").thread_path == "/root/worker"
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT schema_version FROM store_metadata").fetchone()[0] == 14

    rejected_id = "session_rejected"
    rejected = paths.session_db(rejected_id)
    _create_v13_database(rejected, rejected_id)
    with sqlite3.connect(rejected) as connection:
        connection.execute("PRAGMA ignore_check_constraints=ON")
        connection.execute("UPDATE store_metadata SET schema_version=12")
    before = rejected.read_bytes()
    with pytest.raises(RuntimeError, match="Unsupported state.db schema"):
        store.get_session(rejected_id)
    assert rejected.read_bytes() == before


def test_v13_migration_rolls_back_when_sidebar_runtime_is_missing(tmp_path: Path) -> None:
    paths = ClientPaths(tmp_path / "data")
    paths.ensure()
    session_id = "session_orphan"
    database = paths.session_db(session_id)
    _create_v13_database(database, session_id, orphan_sidebar=True)

    with pytest.raises(RuntimeError, match="no main or fork Runtime Thread"):
        SQLiteSessionStore(paths).list_runtime_threads(session_id)
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT schema_version FROM store_metadata").fetchone()[0] == 13
        assert connection.execute("PRAGMA table_info(thread_nodes)").fetchall()[2][1] == "parent_thread_id"


def test_persistent_delegate_runs_in_background_and_auto_delivers_result(tmp_path: Path) -> None:
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

    coordinator = SubagentCoordinator(
        settings=SubagentSettings(max_tasks_per_batch=4, max_workers=2),
        store=store,
        message_queue=queue,
        index=index,
        job_registry=registry,
    )

    def child_factory() -> AgentRunner:
        return AgentRunner(
            _AnswerPlanner(),
            ToolRegistry(list(delegation_tools(4))),
            job_registry=registry,
        )

    coordinator.bind_session(session.session_id, child_factory, session_workspace, project_workspace)
    response = json.loads(
        coordinator.invoke(
            runtime,
            "delegate_tasks",
            {
                "subagent_count": 2,
                "subagent_name": ["one", "two"],
                "subagent_tasks": ["first", "second"],
                "context_transfer_strategy": ["independent", "share"],
            },
        )
    )
    assert response["subagent_count"] == 2
    assert all(item["background_admission"] == "admitted" for item in response["subagents"])

    deadline = monotonic() + 5
    while monotonic() < deadline:
        children = store.list_child_thread_nodes(session.session_id, session.session_id)
        if len(children) == 2 and all(
            store.get_runtime_thread(session.session_id, child.thread_id).running_turn_id is None for child in children
        ):
            break
        sleep(0.01)
    else:
        pytest.fail("background Agent Turns did not finish")

    children = store.list_child_thread_nodes(session.session_id, session.session_id)
    assert {item.thread_path for item in children} == {"/root/one", "/root/two"}
    assert all(item.thread_status == "opening" for item in children)
    child_turns = [
        store.get_node(session.session_id, index.head_for_thread(child.thread_id) or "") for child in children
    ]
    assert all(
        isinstance(turn, RuntimeState)
        and turn.cwd == str(session_workspace.resolve())
        and turn.project_cwd == str(project_workspace.resolve())
        for turn in child_turns
    )
    deadline = monotonic() + 5
    pending = queue.pending_deliveries()
    while monotonic() < deadline and len(pending) < 2:
        sleep(0.01)
        pending = queue.pending_deliveries()
    assert len(pending) == 2
    results = [json.loads(item.envelope.content) for item in pending]
    assert {item["status"] for item in results} == {"success"}
    assert {item["answer"] for item in results} == {"done:first", "done:second"}

    listed = json.loads(coordinator.invoke(runtime, "list_current_node_sub_thread", {}))
    assert {item["thread_path"] for item in listed} == {"/root/one", "/root/two"}
    first_child = children[0]
    coordinator.invoke(
        runtime,
        "set_thread_node_status",
        {"target_thread_id": first_child.thread_id, "thread_status": "closed"},
    )
    with pytest.raises(ToolError, match="closed"):
        coordinator.invoke(
            runtime,
            "send_agent_message",
            {
                "source_thread_id": session.session_id,
                "target_thread_id": first_child.thread_id,
                "subagent_tasks": "rejected while closed",
            },
        )
    coordinator.invoke(
        runtime,
        "set_thread_node_status",
        {"target_thread_id": first_child.thread_id, "thread_status": "opening"},
    )
    with pytest.raises(ToolError, match="does not match"):
        coordinator.invoke(
            runtime,
            "send_agent_message",
            {
                "source_thread_id": "another-thread",
                "target_thread_id": first_child.thread_id,
                "subagent_tasks": "rejected for the wrong source",
            },
        )
    follow_up = json.loads(
        coordinator.invoke(
            runtime,
            "send_agent_message",
            {
                "target_thread_id": first_child.thread_id,
                "subagent_tasks": "follow-up",
            },
        )
    )
    assert follow_up["accepted"] is True and follow_up["target_state"] == "started"
    deadline = monotonic() + 5
    while monotonic() < deadline:
        current = store.get_runtime_thread(session.session_id, first_child.thread_id)
        if current is not None and current.running_turn_id is None and current.current_turn_id == follow_up["turn_id"]:
            break
        sleep(0.01)
    else:
        pytest.fail("idle Agent mailbox delivery did not finish")
    delivered = store.get_node(session.session_id, follow_up["turn_id"])
    assert isinstance(delivered, RuntimeState) and delivered.status == "success"
    assert delivered.user_message["content"][0]["text"] == "follow-up"
    assert delivered.user_message.get("delivery_id") == follow_up["delivery_id"]
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
                    "subagent_count": 1,
                    "subagent_name": ["worker"],
                    "subagent_tasks": ["wait for config"],
                    "context_transfer_strategy": ["independent"],
                },
            )
        )
        thread_id = delegated["subagents"][0]["thread_id"]
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
    store.create_agent_threads(session.session_id, [child])

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
        settings=SubagentSettings(max_tasks_per_batch=4, max_workers=1),
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
    store.create_agent_threads(session.session_id, [child])

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


def test_real_http_sse_redis_subagents_auto_steer_root_and_restart_idle_child(
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
            response = http.post(
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
            assert response.status_code == 200, response.text
            assert response.text.rstrip().endswith('<SSE id="turn_agent_root" type="success"></SSE>')

            store = SQLiteSessionStore(state.paths, state.agent_thread_index)
            children = store.list_child_thread_nodes(sidebar["session_id"], sidebar["thread_id"])
            assert {child.thread_path for child in children} == {"/root/one", "/root/two"}
            root_turn = store.get_node(sidebar["session_id"], "turn_agent_root")
            assert isinstance(root_turn, RuntimeState) and root_turn.status == "success"
            result_messages = [
                str(item.get("text") or "")
                for message in root_turn.data[root_turn.current_data_idx]
                if message.get("role") == "user"
                for item in message.get("content", [])
                if item.get("type") == "text" and '"type": "subagent_initial_result"' in str(item.get("text") or "")
            ]
            assert len(result_messages) == 2
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
                        "target_thread_id": children[0].thread_id,
                        "subagent_tasks": "idle follow-up",
                    },
                )
            )
            assert follow_up["target_state"] == "started"
            deadline = monotonic() + 5
            while monotonic() < deadline:
                child_thread = store.get_runtime_thread(sidebar["session_id"], children[0].thread_id)
                if child_thread is not None and child_thread.running_turn_id is None:
                    break
                sleep(0.01)
            else:
                pytest.fail("idle child follow-up did not finish")
            child_turn = store.get_node(sidebar["session_id"], follow_up["turn_id"])
            assert isinstance(child_turn, RuntimeState) and child_turn.status == "success"
            assert child_turn.user_message.get("delivery_id") == follow_up["delivery_id"]
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
            response = http.post(
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


def test_context_strategies_freeze_share_compact_once_and_keep_independent_isolated(tmp_path: Path) -> None:
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
        settings=SubagentSettings(max_tasks_per_batch=4, max_workers=4),
        store=store,
        message_queue=queue,
        index=index,
        job_registry=registry,
    )
    coordinator.bind_session(
        session.session_id,
        lambda: AgentRunner(
            RecordingPlanner(),
            ToolRegistry(list(delegation_tools(4))),
            job_registry=registry,
        ),
        tmp_path,
    )
    result = json.loads(
        coordinator.invoke(
            runtime,
            "delegate_tasks",
            {
                "subagent_count": 4,
                "subagent_name": ["shared", "solo", "compact-one", "compact-two"],
                "subagent_tasks": ["shared", "solo", "compact-one", "compact-two"],
                "context_transfer_strategy": [
                    "share",
                    "independent",
                    "compaction_share",
                    "compaction_share",
                ],
            },
        )
    )
    assert compaction_calls == 1
    assert {item["effective_strategy"] for item in result["subagents"]} == {
        "share",
        "independent",
        "compaction_share",
    }
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
