from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from time import monotonic, sleep
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from redis import Redis

from backend.api.app import create_app
from backend.api.chat import routes as chat_routes
from backend.api.state import WebAppState
from backend.configuration import ClientPaths
from backend.domain import (
    CHECKPOINT_PREAMBLE,
    AssistantMessage,
    MessageEnvelope,
    RuntimeThread,
    ThreadContext,
    ThreadNode,
    ToolMessage,
)
from backend.domain.runtime_state import NodeWriter, RuntimeRootState, RuntimeState, new_node_id, new_thread_id, utc_iso
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
        sleep(0.1)
        return AssistantMessage(content="waiting for Agent results")


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


def _agent_create(session_id: str, parent: RuntimeState, *, name: str) -> AgentThreadCreate:
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
            session_id,
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
    assert index.thread_for_path(session.session_id, "/root/worker") == child.node.thread_id
    assert index.path_for_thread(child.node.thread_id) == "/root/worker"

    rebuilt = AgentThreadIndex()
    rebuilt.rebuild(SQLiteSessionStore(paths))
    assert rebuilt.threads_for_session(session.session_id) == index.threads_for_session(session.session_id)
    assert rebuilt.path_for_thread(child.node.thread_id) == "/root/worker"


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


def _create_v12_database(path: Path, session_id: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
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
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE store_metadata(session_id TEXT PRIMARY KEY,schema_version INTEGER NOT NULL CHECK(schema_version=12),created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
            CREATE TABLE json_objects(session_id TEXT NOT NULL,namespace TEXT NOT NULL,object_id TEXT NOT NULL,payload_json TEXT NOT NULL,updated_at TEXT NOT NULL,PRIMARY KEY(session_id,namespace,object_id));
            CREATE TABLE workspace_files(session_id TEXT NOT NULL,relative_path TEXT NOT NULL,size INTEGER NOT NULL,sha256 TEXT NOT NULL,mtime_ns INTEGER NOT NULL,PRIMARY KEY(session_id,relative_path));
            CREATE TABLE sandbox_approvals(request_hash TEXT PRIMARY KEY,session_id TEXT NOT NULL,command_hash TEXT NOT NULL,cwd_hash TEXT NOT NULL,permission_target TEXT NOT NULL,network_target_hash TEXT NOT NULL,command_summary TEXT NOT NULL,cwd_summary TEXT NOT NULL,created_at TEXT NOT NULL);
            """
        )
        connection.execute(
            "INSERT INTO store_metadata VALUES (?,12,?,?)",
            (session_id, utc_iso(), utc_iso()),
        )
        session_payload = {
            "session_id": session_id,
            "title": "migrated",
            "created_at": utc_iso(),
            "updated_at": utc_iso(),
            "title_is_custom": False,
        }
        for namespace, object_id, payload in (
            ("session", session_id, session_payload),
            ("runtime_node", root.id, root.to_dict()),
            ("runtime_node", main.id, main.to_dict()),
            ("runtime_node", fork.id, fork.to_dict()),
        ):
            connection.execute(
                "INSERT INTO json_objects VALUES (?,?,?,?,?)",
                (session_id, namespace, object_id, json.dumps(payload), utc_iso()),
            )


def test_v12_migrates_to_v13_and_v11_is_left_untouched(tmp_path: Path) -> None:
    paths = ClientPaths(tmp_path / "data")
    paths.ensure()
    session_id = "session_migrate"
    database = paths.session_db(session_id)
    _create_v12_database(database, session_id)

    store = SQLiteSessionStore(paths)
    threads = store.list_runtime_threads(session_id)
    assert {(item.thread_id, item.origin_kind) for item in threads} == {
        (session_id, "main"),
        ("thread_fork", "fork"),
    }
    assert store.get_thread_node(session_id, session_id).thread_task == "first task"
    assert store.get_thread_node(session_id, "thread_fork") is None
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT schema_version FROM store_metadata").fetchone()[0] == 13

    rejected_id = "session_rejected"
    rejected = paths.session_db(rejected_id)
    _create_v12_database(rejected, rejected_id)
    with sqlite3.connect(rejected) as connection:
        connection.execute("PRAGMA ignore_check_constraints=ON")
        connection.execute("UPDATE store_metadata SET schema_version=11")
    before = rejected.read_bytes()
    with pytest.raises(RuntimeError, match="Unsupported state.db schema"):
        store.get_session(rejected_id)
    assert rejected.read_bytes() == before


def test_v12_migration_rolls_back_when_running_head_is_ambiguous(tmp_path: Path) -> None:
    paths = ClientPaths(tmp_path / "data")
    paths.ensure()
    session_id = "session_ambiguous"
    database = paths.session_db(session_id)
    _create_v12_database(database, session_id)
    with sqlite3.connect(database) as connection:
        raw = connection.execute(
            "SELECT payload_json FROM json_objects WHERE namespace='runtime_node' AND object_id='main_turn'"
        ).fetchone()[0]
        first = json.loads(raw)
        first["status"] = "running"
        second = {**first, "id": "main_turn_two", "timestamp": utc_iso()}
        connection.execute(
            "UPDATE json_objects SET payload_json=? WHERE namespace='runtime_node' AND object_id='main_turn'",
            (json.dumps(first),),
        )
        connection.execute(
            "INSERT INTO json_objects VALUES (?,?,?,?,?)",
            (session_id, "runtime_node", second["id"], json.dumps(second), utc_iso()),
        )

    with pytest.raises(RuntimeError, match="only one running Turn"):
        SQLiteSessionStore(paths).list_runtime_threads(session_id)
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT schema_version FROM store_metadata").fetchone()[0] == 12
        assert (
            connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='runtime_threads'").fetchone()
            is None
        )


def test_persistent_delegate_runs_in_background_and_auto_delivers_result(tmp_path: Path) -> None:
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
        )
    )
    parent_runner = AgentRunner(_AnswerPlanner(), ToolRegistry())
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

    coordinator.bind_session(session.session_id, child_factory, tmp_path)
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
