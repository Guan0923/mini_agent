"""Reliable SQLite-outbox to Redis fan-out and replayable browser SSE."""

from __future__ import annotations

import asyncio
import html
import json
import threading
from collections.abc import AsyncIterator
from time import monotonic

from backend.domain.runtime_state import NodeFrame, RuntimeState
from backend.storage.sqlite import SQLiteSessionStore

from .agent_report_projection import project_frame


def _store(state) -> SQLiteSessionStore:
    return SQLiteSessionStore(state.paths, getattr(state, "agent_thread_index", None))


def publish_outbox_event(state, event: dict[str, object], *, recover_terminal: bool = False) -> None:
    event_id = str(event.get("event_id") or "")
    session_id = str(event.get("session_id") or "")
    thread_id = str(event.get("thread_id") or "")
    turn_id = str(event.get("turn_id") or "")
    frame_payload = event.get("frame")
    current_payload = event.get("current")
    if not event_id or not all((session_id, thread_id, turn_id)):
        raise ValueError("Runtime outbox event identifiers are required.")
    if not isinstance(frame_payload, dict) or not isinstance(current_payload, dict):
        raise ValueError("Runtime outbox event requires frame and current payloads.")
    frame = NodeFrame.from_dict(frame_payload, event_id=event_id)
    current = RuntimeState.from_dict(current_payload)
    store = _store(state)
    payload = project_frame(store, frame, current)
    state.runtime_event_stream.publish(
        event_id=event_id,
        turn_id=turn_id,
        thread_id=thread_id,
        sequence=int(event.get("sequence") or 0),
        payload=payload,
    )
    store.ack_runtime_event(session_id, event_id)
    active_streams = getattr(state, "active_turn_streams", {})
    execution_active = isinstance(active_streams, dict) and turn_id in active_streams
    if recover_terminal and not execution_active and current.status in {"success", "paused", "failed"}:
        # A live execution still has synchronous post-finalization work (for
        # example the first-Turn title) before it publishes the terminal.  The
        # relay owns terminal recovery only after no execution is active.
        publish_terminal(
            state,
            session_id=session_id,
            thread_id=thread_id,
            turn_id=turn_id,
            terminal_type="success" if current.status in {"success", "paused"} else "failed",
        )


def publish_frame(state, frame: NodeFrame, current: RuntimeState) -> None:
    store = _store(state)
    event = store.runtime_event(frame.session_id, frame.event_id)
    if event is None:
        # The relay can publish and ACK between NodeWriter's transaction and
        # this synchronous callback. The Redis receipt is the durable proof
        # that this exact frame already reached both fan-out streams.
        if state.runtime_event_stream.has_event(frame.event_id):
            return
        raise RuntimeError("Persisted Runtime frame is missing its outbox event.")
    publish_outbox_event(state, event)


def publish_terminal(
    state,
    *,
    session_id: str,
    thread_id: str,
    turn_id: str,
    terminal_type: str,
    message: str = "",
) -> None:
    _node, sequence = _store(state).runtime_stream_snapshot(session_id, turn_id)
    state.runtime_event_stream.publish(
        # The execution thread and the recovery relay may observe the same
        # committed terminal frame concurrently.  A deterministic receipt
        # makes both publications one Redis event instead of two terminals.
        event_id=f"terminal:{session_id}:{turn_id}:{sequence + 1}:{terminal_type}",
        turn_id=turn_id,
        thread_id=thread_id,
        sequence=sequence + 1,
        payload={
            "type": "turn.terminal",
            "session_id": session_id,
            "thread_id": thread_id,
            "turn_id": turn_id,
            "terminal_type": terminal_type,
            "message": message,
        },
    )


class RuntimeEventRelay:
    def __init__(self, state) -> None:
        self.state = state
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="runtime-event-relay", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def _run(self) -> None:
        while not self._stop.wait(0.25):
            store = _store(self.state)
            try:
                summaries = store.list_sessions(state="all")
                for summary in summaries:
                    for event in store.pending_runtime_events(summary.session_id):
                        if self._stop.is_set():
                            return
                        publish_outbox_event(self.state, event, recover_terminal=True)
            except Exception:
                # The canonical outbox remains intact; the next pass retries.
                continue


def _terminal_envelope(
    turn_id: str,
    terminal_type: str,
    message: str = "",
    *,
    event_id: str = "0-0",
) -> str:
    safe_id = html.escape(turn_id, quote=True)
    safe_type = html.escape(terminal_type, quote=True)
    safe_message = html.escape(message, quote=False)
    return f'id: {event_id}\ndata: <SSE id="{safe_id}" type="{safe_type}">{safe_message}</SSE>\n\n'


def _terminal_for_node(node: RuntimeState) -> tuple[str, str] | None:
    if node.status == "running":
        return None
    return ("success" if node.status in {"success", "paused"} else "failed", "")


def _node_has_delivery(node: RuntimeState, delivery_id: str) -> bool:
    return any(
        message.get("delivery_id") == delivery_id
        or any(item.get("delivery_id") == delivery_id for item in message.get("content", []))
        for version in node.data
        for message in version
    )


def _matching_terminal(
    payload: dict[str, object],
    *,
    session_id: str,
    thread_id: str,
    turn_id: str,
) -> tuple[str, str] | None:
    if (
        payload.get("type") != "turn.terminal"
        or payload.get("session_id") != session_id
        or payload.get("thread_id") != thread_id
        or payload.get("turn_id") != turn_id
    ):
        return None
    return (
        str(payload.get("terminal_type") or "failed"),
        str(payload.get("message") or ""),
    )


def _turn_continuation(store: SQLiteSessionStore, session_id: str, thread_id: str, turn_id: str) -> list[RuntimeState]:
    turns = [
        node for node in store.load_nodes(session_id) if isinstance(node, RuntimeState) and node.thread_id == thread_id
    ]
    by_id = {node.id: node for node in turns}

    def descends_from_requested(node: RuntimeState) -> bool:
        current: RuntimeState | None = node
        seen: set[str] = set()
        while current is not None and current.id not in seen:
            if current.id == turn_id:
                return True
            seen.add(current.id)
            parent = by_id.get(current.parent_id)
            current = parent if isinstance(parent, RuntimeState) else None
        return False

    return [node for node in turns if descends_from_requested(node)]


async def turn_sse(
    state,
    session_id: str,
    thread_id: str,
    turn_id: str,
    last_event_id: str | None = None,
    delivery_id: str | None = None,
) -> AsyncIterator[str]:
    stream = state.runtime_event_stream
    # One execution may hand off from a Plan Turn to compact/agent Turns. The
    # Thread stream is the durable equivalent of the former process-local
    # ActiveTurnStream aliases and keeps that execution observable end-to-end.
    cursor = last_event_id or await asyncio.to_thread(stream.latest_thread_id, thread_id)
    heartbeat_at = monotonic() + 15.0
    while True:
        store = _store(state)
        node, _baseline_sequence = store.runtime_stream_snapshot(session_id, turn_id)
        # Existing Turns are reused by rewind. Do not mistake the old sealed
        # version for the newly accepted delivery while the Redis worker is
        # still between claim and SQLite admission.
        if node is not None and (not delivery_id or _node_has_delivery(node, delivery_id)):
            break
        latest_turn_event = await asyncio.to_thread(stream.latest_turn_event, turn_id) if node is None else None
        if latest_turn_event is not None:
            terminal = _matching_terminal(
                latest_turn_event.payload,
                session_id=session_id,
                thread_id=thread_id,
                turn_id=turn_id,
            )
            if terminal is not None:
                terminal_cursor = await asyncio.to_thread(stream.latest_thread_id, thread_id)
                yield _terminal_envelope(turn_id, *terminal, event_id=terminal_cursor)
                return
        entries = await asyncio.to_thread(stream.read_thread, thread_id, cursor, block_ms=50)
        for entry in entries:
            cursor = entry.stream_id
            terminal = _matching_terminal(
                entry.payload,
                session_id=session_id,
                thread_id=thread_id,
                turn_id=turn_id,
            )
            if terminal is not None:
                yield _terminal_envelope(
                    turn_id,
                    *terminal,
                    event_id=cursor,
                )
                return
        if monotonic() >= heartbeat_at:
            heartbeat_at = monotonic() + 15.0
            yield ": heartbeat\n\n"
        await asyncio.sleep(0.05)
    canonical_candidates = _turn_continuation(store, session_id, thread_id, turn_id) or [node]
    canonical_turns: list[RuntimeState] = []
    baseline_sequences: dict[str, int] = {}
    for candidate in canonical_candidates:
        current, baseline_sequence = store.runtime_stream_snapshot(session_id, candidate.id)
        canonical_turns.append(current or candidate)
        baseline_sequences[candidate.id] = baseline_sequence
    active_streams = getattr(state, "active_turn_streams", {})
    execution_active = isinstance(active_streams, dict) and turn_id in active_streams
    terminal = _terminal_for_node(canonical_turns[-1])
    if terminal is not None and not execution_active:
        # No more Runtime frames can be committed for this execution. Advance
        # the browser cursor to the terminal tail represented by the SQLite
        # snapshot so a reconnect does not replay already-folded frames.
        cursor = await asyncio.to_thread(stream.latest_thread_id, thread_id)
    local_revisions: dict[str, int] = {}
    for canonical in canonical_turns:
        local_revisions[canonical.id] = 0
        snapshot = project_frame(store, NodeFrame.snapshot(canonical), canonical)
        snapshot["revision"] = 0
        yield f"id: {cursor}\ndata: {json.dumps(snapshot, ensure_ascii=False, separators=(',', ':'))}\n\n"
    if terminal is not None and not execution_active:
        yield _terminal_envelope(turn_id, *terminal, event_id=cursor)
        return

    heartbeat_at = monotonic() + 15.0
    while True:
        entries = await asyncio.to_thread(stream.read_thread, thread_id, cursor, block_ms=1000)
        if not entries:
            if monotonic() >= heartbeat_at:
                heartbeat_at = monotonic() + 15.0
                yield ": heartbeat\n\n"
            continue
        for entry in entries:
            cursor = entry.stream_id
            payload = dict(entry.payload)
            if payload.get("type") == "turn.terminal":
                yield _terminal_envelope(
                    turn_id,
                    str(payload.get("terminal_type") or "failed"),
                    str(payload.get("message") or ""),
                    event_id=cursor,
                )
                return
            payload_turn_id = ""
            if payload.get("type") == "turn.snapshot":
                turn = payload.get("turn")
                payload_turn_id = str(turn.get("id") or "") if isinstance(turn, dict) else ""
            elif payload.get("type") == "turn.delta":
                payload_turn_id = str(payload.get("turn_id") or "")
            if not payload_turn_id or entry.sequence <= baseline_sequences.get(payload_turn_id, 0):
                continue
            if payload.get("type") == "turn.snapshot":
                local_revisions[payload_turn_id] = 0
                payload["revision"] = 0
            elif payload.get("type") == "turn.delta":
                if payload_turn_id not in local_revisions:
                    current, current_sequence = _store(state).runtime_stream_snapshot(session_id, payload_turn_id)
                    if current is None:
                        continue
                    baseline_sequences[payload_turn_id] = current_sequence
                    local_revisions[payload_turn_id] = 0
                    snapshot = project_frame(_store(state), NodeFrame.snapshot(current), current)
                    snapshot["revision"] = 0
                    yield f"id: {cursor}\ndata: {json.dumps(snapshot, ensure_ascii=False, separators=(',', ':'))}\n\n"
                    continue
                local_revisions[payload_turn_id] += 1
                payload["revision"] = local_revisions[payload_turn_id]
            else:
                continue
            yield f"id: {cursor}\ndata: {json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}\n\n"


async def thread_sse(state, session_id: str, thread_id: str, last_event_id: str | None = None) -> AsyncIterator[str]:
    stream = state.runtime_event_stream
    cursor = last_event_id or await asyncio.to_thread(stream.latest_thread_id, thread_id)
    yield f"id: {cursor}\ndata: {json.dumps({'type': 'thread.ready', 'session_id': session_id, 'thread_id': thread_id}, separators=(',', ':'))}\n\n"
    store = _store(state)
    canonical_turns = [
        node for node in store.load_nodes(session_id) if isinstance(node, RuntimeState) and node.thread_id == thread_id
    ]
    baseline_sequences: dict[str, int] = {}
    refreshed_turns: list[RuntimeState] = []
    for candidate in canonical_turns:
        current, baseline_sequence = store.runtime_stream_snapshot(session_id, candidate.id)
        node = current or candidate
        refreshed_turns.append(node)
        baseline_sequences[node.id] = baseline_sequence
        snapshot = project_frame(store, NodeFrame.snapshot(node), node)
        snapshot["revision"] = 0
        yield f"id: {cursor}\ndata: {json.dumps(snapshot, ensure_ascii=False, separators=(',', ':'))}\n\n"
    heartbeat_at = monotonic() + 15.0
    revisions: dict[str, int] = {node.id: 0 for node in refreshed_turns}
    while True:
        entries = await asyncio.to_thread(stream.read_thread, thread_id, cursor, block_ms=1000)
        if not entries:
            if monotonic() >= heartbeat_at:
                heartbeat_at = monotonic() + 15.0
                yield ": heartbeat\n\n"
            continue
        for entry in entries:
            cursor = entry.stream_id
            payload = dict(entry.payload)
            if payload.get("type") == "turn.terminal":
                payload = {
                    "type": "turn.terminal",
                    "session_id": session_id,
                    "thread_id": thread_id,
                    "turn_id": str(payload.get("turn_id") or ""),
                    "status": "success" if payload.get("terminal_type") == "success" else "failed",
                }
            elif payload.get("type") == "turn.snapshot":
                turn = payload.get("turn")
                turn_id = str(turn.get("id") or "") if isinstance(turn, dict) else ""
                if entry.sequence <= baseline_sequences.get(turn_id, 0):
                    continue
                baseline_sequences[turn_id] = entry.sequence
                revisions[turn_id] = 0
                payload["revision"] = 0
            elif payload.get("type") == "turn.delta":
                turn_id = str(payload.get("turn_id") or "")
                if entry.sequence <= baseline_sequences.get(turn_id, 0):
                    continue
                revisions[turn_id] = revisions.get(turn_id, 0) + 1
                payload["revision"] = revisions[turn_id]
            else:
                continue
            yield f"id: {cursor}\ndata: {json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}\n\n"


__all__ = [
    "RuntimeEventRelay",
    "publish_frame",
    "publish_terminal",
    "thread_sse",
    "turn_sse",
]
