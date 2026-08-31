from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from backend.api.app import create_app
from backend.api.session_store import session_store as web_session_store
from backend.api.state import WebAppState
from backend.api.terminal_manager import TERMINAL_DISCONNECT_SECONDS, TerminalManager, TerminalSession
from backend.domain.right_panel import RightPanelWindow
from backend.domain.runtime_state import RuntimeState, RuntimeStateTree
from backend.domain.state import utc_now
from backend.storage.terminal_stream import MAX_TERMINAL_CHUNKS, RedisTerminalOutputStream, TerminalOutputChunk
from tests.local_store import session_store


def _version(prompt: str, assistant_items: list[dict[str, object]]) -> list[dict[str, object]]:
    return [
        {"role": "user", "content": [{"type": "text", "text": prompt, "status": "success"}]},
        {"role": "assistant", "content": assistant_items},
    ]


def _running_source(store, session_id: str, cwd: Path, *, turn_id: str = "turn_source") -> RuntimeState:
    root = store.ensure_root_node(session_id, id=f"{turn_id}_root")
    source = RuntimeState.create(
        session_id=session_id,
        thread_id=session_id,
        id=turn_id,
        parent=root,
        user_content="selected",
        provider_name="provider-copy",
        model={
            "current_model": "model-copy",
            "context_length": 8192,
            "output_length": 1024,
            "temperature": 0.2,
            "reasoning_effort": "high",
        },
        permission_mode="workspace_write",
        running_mode="plan",
        cwd=str(cwd),
        data=[
            _version("old-version", [{"type": "text", "text": "must-not-copy", "status": "success"}]),
            _version(
                "selected-version",
                [
                    {"type": "text", "text": "kept", "status": "success"},
                    {"type": "text", "text": "failed", "status": "failed"},
                    {"type": "reasoning", "text": "partial", "status": "running"},
                    {
                        "type": "tool_call",
                        "call_id": "complete",
                        "name": "read_file",
                        "arguments": {},
                        "status": "success",
                    },
                    {
                        "type": "tool_result",
                        "call_id": "complete",
                        "tool": "read_file",
                        "content": "done",
                        "status": "success",
                    },
                    {
                        "type": "tool_call",
                        "call_id": "orphan",
                        "name": "glob",
                        "arguments": {},
                        "status": "success",
                    },
                    {
                        "type": "tool_result",
                        "call_id": "reversed",
                        "content": "too early",
                        "status": "success",
                    },
                    {
                        "type": "tool_call",
                        "call_id": "reversed",
                        "name": "read_file",
                        "arguments": {},
                        "status": "success",
                    },
                ],
            ),
        ],
    )
    payload = source.to_dict()
    payload["current_data_idx"] = 1
    selected = RuntimeState.from_dict(payload)
    store.create_node(selected)
    return selected


def test_side_chat_anchor_is_an_atomic_filtered_running_snapshot(tmp_path: Path) -> None:
    store = session_store(tmp_path / "data")
    session = store.create_session("main")
    source = _running_source(store, session.session_id, tmp_path)

    anchor = store.build_side_chat_anchor(source.id, new_turn_id="turn_anchor", thread_id="thread_side")

    assert store.find_node(source.id).status == "running"
    assert anchor.id != source.id and anchor.thread_id != source.thread_id
    assert anchor.parent_id == source.parent_id
    assert anchor.parent_thread_id == source.parent_thread_id
    assert anchor.status == "paused"
    assert anchor.current_data_idx == 0 and len(anchor.data) == 1
    assert anchor.provider_name == source.provider_name
    assert anchor.model == source.model
    assert anchor.permission_mode == source.permission_mode
    assert anchor.running_mode == source.running_mode
    assert anchor.cwd == source.cwd
    assert anchor.user_message["content"][0]["text"] == "selected-version"
    assert [item.get("type") for item in anchor.assistant_items] == ["text", "tool_call", "tool_result"]
    assert {item.get("call_id") for item in anchor.assistant_items if item.get("call_id")} == {"complete"}

    now = utc_now()
    window = RightPanelWindow(
        id="window_side",
        session_id=session.session_id,
        kind="side_chat",
        title="侧聊 1",
        position=0,
        created_at=now,
        updated_at=now,
        thread_id=anchor.thread_id,
        anchor_turn_id=anchor.id,
    )
    store.create_side_chat_window(window, anchor)
    reopened = session_store(tmp_path / "data")
    assert reopened.get_right_panel_window(session.session_id, window.id) == window
    assert reopened.find_node(anchor.id) == anchor
    assert [node.id for node in RuntimeStateTree(reopened.load_nodes(session.session_id)).ancestors(anchor)] == [
        source.parent_id,
        anchor.id,
    ]


def test_right_panel_layout_is_isolated_per_session_and_keeps_width_when_collapsed(tmp_path: Path) -> None:
    store = session_store(tmp_path / "data")
    first = store.create_session("first")
    second = store.create_session("second")

    assert store.get_right_panel_state(first.session_id).width == 420
    assert store.get_right_panel_state(first.session_id).collapsed is True
    store.save_right_panel_state(first.session_id, width=640, collapsed=False)
    store.save_right_panel_state(first.session_id, collapsed=True)

    assert store.get_right_panel_state(first.session_id).width == 640
    assert store.get_right_panel_state(first.session_id).collapsed is True
    assert store.get_right_panel_state(second.session_id).width == 420
    assert store.get_right_panel_state(second.session_id).collapsed is True


def test_side_chat_api_stays_out_of_sidebar_and_close_keeps_empty_panel_open(tmp_path: Path) -> None:
    state = WebAppState(tmp_path / ".mini_agent")
    with TestClient(create_app(state)) as client:
        sidebar = client.post("/api/sidebar-threads", json={}).json()
        store = web_session_store(state)
        source = _running_source(store, sidebar["session_id"], tmp_path)

        created = client.post(
            f"/api/right-panel/{sidebar['session_id']}/side-chats",
            json={"source_turn_id": source.id},
        )
        assert created.status_code == 201
        window = created.json()["window"]
        assert window["thread_id"] != sidebar["thread_id"]
        assert [item["thread_id"] for item in client.get("/api/sidebar-threads").json()] == [sidebar["thread_id"]]
        assert client.get(f"/api/sidebar-threads/{window['thread_id']}/queued-messages").status_code == 200
        assert client.patch(f"/api/sidebar-threads/{window['thread_id']}", json={"title": "bad"}).status_code == 404

        anchor = store.find_node(window["anchor_turn_id"])
        assert isinstance(anchor, RuntimeState)
        child = RuntimeState.create(
            session_id=sidebar["session_id"],
            thread_id=window["thread_id"],
            id="turn_side_running",
            parent=anchor,
            user_content="continue",
            cwd=str(tmp_path),
        )
        store.create_node(child)

        closed = client.delete(f"/api/right-panel/{sidebar['session_id']}/windows/{window['id']}")
        assert closed.status_code == 204
        paused = store.find_node(child.id)
        assert isinstance(paused, RuntimeState) and paused.status == "paused"
        payload = client.get(f"/api/right-panel/{sidebar['session_id']}").json()
        assert payload["windows"] == []
        assert payload["state"]["collapsed"] is False
        assert payload["state"]["active_window_id"] is None
        assert client.get(f"/api/sidebar-threads/{window['thread_id']}/queued-messages").status_code == 404


@pytest.mark.skipif(os.name != "nt", reason="Windows terminal test")
def test_terminal_creation_fails_closed_without_redis_and_restart_drops_stale_metadata(tmp_path: Path) -> None:
    state = WebAppState(tmp_path / ".mini_agent")
    with TestClient(create_app(state)) as client:
        sidebar = client.post("/api/sidebar-threads", json={}).json()
        store = web_session_store(state)
        source = _running_source(store, sidebar["session_id"], tmp_path)
        unavailable = client.post(
            f"/api/right-panel/{sidebar['session_id']}/terminals",
            json={"source_turn_id": source.id},
        )
        assert unavailable.status_code == 503
        assert unavailable.json()["detail"] == "message_queue_unavailable"

        now = utc_now()
        stale = RightPanelWindow(
            id="window_stale_terminal",
            session_id=sidebar["session_id"],
            kind="terminal",
            title="PowerShell 1",
            position=0,
            created_at=now,
            updated_at=now,
            terminal_id="terminal_from_old_backend",
            terminal_type="powershell",
            cwd=str(tmp_path),
        )
        store.create_right_panel_window(stale)
        store.save_right_panel_state(sidebar["session_id"], collapsed=False, active_window_id=stale.id)

        recovered = client.get(f"/api/right-panel/{sidebar['session_id']}").json()
        assert recovered["windows"] == []
        assert recovered["state"]["active_window_id"] is None
        deleted = store.get_right_panel_window(sidebar["session_id"], stale.id)
        assert deleted is not None and deleted.deleted_at is not None


class _FakePipeline:
    def __init__(self, client: _FakeRedis) -> None:
        self.client = client

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def expire(self, key: str, seconds: int) -> None:
        self.client.expirations[key] = seconds

    def execute(self) -> list[object]:
        return []


class _FakeRedis:
    def __init__(self) -> None:
        self.streams: dict[str, list[tuple[str, dict[str, object]]]] = {}
        self.sequences: dict[str, int] = {}
        self.expirations: dict[str, int] = {}

    def register_script(self, _script: str):
        def append(*, keys: list[str], args: list[object]) -> int:
            stream, sequence_key = keys
            sequence = self.sequences.get(sequence_key, 0) + 1
            self.sequences[sequence_key] = sequence
            rows = self.streams.setdefault(stream, [])
            rows.append((f"{sequence}-0", {"sequence": sequence, "data": args[0]}))
            del rows[: max(0, len(rows) - int(args[1]))]
            return sequence

        return append

    def ping(self) -> bool:
        return True

    def xrange(self, stream: str, **_kwargs):
        return list(self.streams.get(stream, []))

    def pipeline(self) -> _FakePipeline:
        return _FakePipeline(self)

    def delete(self, *keys: str) -> None:
        for key in keys:
            self.streams.pop(key, None)
            self.sequences.pop(key, None)
            self.expirations.pop(key, None)


def test_terminal_stream_chunks_and_strictly_keeps_the_latest_256_entries() -> None:
    stream = RedisTerminalOutputStream(_FakeRedis(), key_prefix="test")
    split = stream.append("terminal", "x" * (16 * 1024 + 5))
    assert [len(item.data) for item in split] == [16 * 1024, 5]
    stream.delete("terminal")

    for index in range(MAX_TERMINAL_CHUNKS + 1):
        stream.append("terminal", str(index))
    replay = stream.after("terminal", 0)
    assert len(replay) == MAX_TERMINAL_CHUNKS
    assert replay[0].sequence == 2 and replay[-1].sequence == MAX_TERMINAL_CHUNKS + 1


@dataclass
class _FakePty:
    alive: bool = True
    exitstatus: int = 0
    closed: bool = False
    pid: int = 43210

    def isalive(self) -> bool:
        return self.alive

    def close(self, *, force: bool) -> None:
        assert force is True
        self.closed = True
        self.alive = False


class _FakeOutput:
    def __init__(self) -> None:
        self.expired: list[str] = []
        self.deleted: list[str] = []

    def expire(self, terminal_id: str) -> None:
        self.expired.append(terminal_id)

    def delete(self, terminal_id: str) -> None:
        self.deleted.append(terminal_id)


class _FakeTimer:
    instances: list[_FakeTimer] = []

    def __init__(self, delay: int, callback, args: tuple[str, ...]) -> None:
        self.delay = delay
        self.callback = callback
        self.args = args
        self.cancelled = False
        self.daemon = False
        self.instances.append(self)

    def start(self) -> None:
        return None

    def cancel(self) -> None:
        self.cancelled = True

    def fire(self) -> None:
        if not self.cancelled:
            self.callback(*self.args)


@pytest.mark.skipif(os.name != "nt", reason="Windows terminal cleanup test")
def test_terminal_disconnect_reconnect_and_explicit_cleanup(monkeypatch) -> None:
    monkeypatch.setattr("backend.api.terminal_manager.Timer", _FakeTimer)
    taskkill: list[list[str]] = []
    monkeypatch.setattr(
        "backend.api.terminal_manager.subprocess.run",
        lambda argv, **_kwargs: taskkill.append(argv),
    )
    _FakeTimer.instances.clear()
    manager = TerminalManager(SimpleNamespace())
    process = _FakePty()
    output = _FakeOutput()
    session = TerminalSession("terminal", "cmd", "C:\\work", process, output)  # type: ignore[arg-type]
    manager._sessions[session.id] = session

    manager.connect(session.id)
    manager.disconnect(session.id)
    first = _FakeTimer.instances[-1]
    assert first.delay == TERMINAL_DISCONNECT_SECONDS == 30 * 60
    assert output.expired == [session.id]

    manager.connect(session.id)
    assert first.cancelled is True
    manager.disconnect(session.id)
    _FakeTimer.instances[-1].fire()
    assert manager.get(session.id) is None
    assert process.closed is True
    assert output.deleted == [session.id]
    assert taskkill == [["taskkill.exe", "/PID", "43210", "/T", "/F"]]


class _WsTerminal:
    def __init__(self) -> None:
        self.exit_code: int | None = None

    def payload(self) -> dict[str, object]:
        return {
            "id": "terminal_ws",
            "terminal_type": "cmd",
            "terminal_label": "cmd",
            "cwd": "C:\\work",
            "last_sequence": 3,
            "exit_code": self.exit_code,
            "alive": self.exit_code is None,
        }


class _WsTerminalManager:
    def __init__(self) -> None:
        self.session = _WsTerminal()
        self.ready = Event()
        self.inputs: list[str] = []
        self.resizes: list[tuple[int, int]] = []
        self.after: list[int] = []
        self.connects = 0
        self.disconnects = 0

    def connect(self, terminal_id: str) -> _WsTerminal:
        assert terminal_id == "terminal_ws"
        self.connects += 1
        return self.session

    def disconnect(self, terminal_id: str) -> None:
        assert terminal_id == "terminal_ws"
        self.disconnects += 1

    def write(self, terminal_id: str, data: str) -> None:
        assert terminal_id == "terminal_ws"
        self.inputs.append(data)
        if self.resizes:
            self.ready.set()

    def resize(self, terminal_id: str, cols: int, rows: int) -> None:
        assert terminal_id == "terminal_ws"
        self.resizes.append((cols, rows))
        if self.inputs:
            self.ready.set()

    def wait_after(self, terminal_id: str, sequence: int) -> list[TerminalOutputChunk]:
        assert terminal_id == "terminal_ws"
        self.after.append(sequence)
        assert self.ready.wait(2)
        self.session.exit_code = 7
        return [TerminalOutputChunk(4, "echoed")]

    def get(self, terminal_id: str) -> _WsTerminal | None:
        return self.session if terminal_id == "terminal_ws" else None

    def close_all(self) -> None:
        return None


def test_terminal_websocket_checks_origin_and_carries_input_resize_cursor_and_exit(tmp_path: Path) -> None:
    state = WebAppState(tmp_path / ".mini_agent")
    manager = _WsTerminalManager()
    state.terminal_manager = manager  # type: ignore[assignment]
    with TestClient(create_app(state)) as client:
        with pytest.raises(WebSocketDisconnect) as rejected:
            with client.websocket_connect(
                "/api/right-panel/terminals/terminal_ws/ws",
                headers={"Origin": "https://outside.example"},
            ):
                pass
        assert rejected.value.code == 1008
        assert manager.connects == 0

        with client.websocket_connect(
            "/api/right-panel/terminals/terminal_ws/ws?after_sequence=3",
            headers={"Origin": "http://127.0.0.1:5173"},
        ) as websocket:
            assert websocket.receive_json()["type"] == "ready"
            websocket.send_json({"type": "input", "data": "dir\r"})
            websocket.send_json({"type": "resize", "cols": 100, "rows": 40})
            assert websocket.receive_json() == {"type": "output", "sequence": 4, "data": "echoed"}
            assert websocket.receive_json() == {"type": "exit", "code": 7, "last_sequence": 4}

    assert manager.inputs == ["dir\r"]
    assert manager.resizes == [(100, 40)]
    assert manager.after[0] == 3
    assert manager.disconnects == 1
