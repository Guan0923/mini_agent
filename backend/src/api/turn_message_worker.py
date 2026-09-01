"""Consume reliable user-message commands and admit their Turn executions."""

from __future__ import annotations

import os
import threading

from backend.domain.runtime_state import RuntimeState
from backend.storage.sqlite import SQLiteSessionStore

from .routes.turn_models import TurnExecutionConfig
from .routes.turn_support import _stream_turn


class TurnMessageWorker:
    def __init__(self, state) -> None:
        self.state = state
        self.consumer = f"turn-start-{os.getpid()}"
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="turn-message-worker", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def _run(self) -> None:
        recover = True
        while not self._stop.wait(0.05):
            try:
                claimed = self.state.message_queue.claim_turn_start(self.consumer, recover=recover)
                recover = False
                if claimed is None:
                    continue
                self._start(claimed)
            except Exception:
                # Claimed messages remain pending and are reclaimed after the
                # worker-safe idle boundary. Canonical failures are persisted
                # by the Runtime once admission has succeeded.
                continue

    def _start(self, claimed) -> None:
        envelope = claimed.envelope
        payload = envelope.payload
        store = SQLiteSessionStore(self.state.paths, getattr(self.state, "agent_thread_index", None))
        operation = str(payload.get("operation") or "create")
        existing = store.find_node(envelope.target_id)
        if isinstance(existing, RuntimeState) and any(
            message.get("delivery_id") == envelope.delivery_id for version in existing.data for message in version
        ):
            self.state.message_queue.ack(claimed)
            return
        if operation == "create" and isinstance(existing, RuntimeState):
            return
        config_payload = payload.get("config")
        config = TurnExecutionConfig.model_validate(config_payload if isinstance(config_payload, dict) else {})
        if operation == "rewind":
            item: dict[str, object] = {"type": "text", "text": envelope.content, "status": "success"}
            if envelope.references:
                item["references"] = list(envelope.references)
            rewound = store.append_turn_version(
                envelope.target_id,
                item,
                delivery_id=envelope.delivery_id,
            )
            _stream_turn(
                self.state,
                session_id=envelope.session_id,
                thread_id=envelope.thread_id,
                turn_id=envelope.target_id,
                prompt=envelope.content,
                source_id=rewound.id,
                config=config,
                references=list(envelope.references),
                adopt_existing=True,
                initial_delivery=claimed,
                stream_response=False,
            )
            return
        if operation != "create":
            raise ValueError("Unsupported queued Turn message operation.")
        _stream_turn(
            self.state,
            session_id=envelope.session_id,
            thread_id=envelope.thread_id,
            turn_id=envelope.target_id,
            prompt=envelope.content,
            source_id=str(payload.get("parent_id") or "") or None,
            config=config,
            references=list(envelope.references),
            initial_delivery=claimed,
            stream_response=False,
        )


__all__ = ["TurnMessageWorker"]
